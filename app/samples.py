"""Test drafts, so the sidebar has something to show on a fresh install.

The people and companies are made up, and every sample is marked as one on screen.
Each new account gets them once. Deleting one does not bring it back.
"""
import json
import time
from datetime import date, timedelta
from pathlib import Path

from . import checks, config, db, pipeline, scoring

DAY = 86400
ART_FILE = Path(__file__).with_name("sample_art.json")  # drawn once by the real AI step
GOOD_READERS = {"only_them": True, "same_idea": True, "would_open": True, "kind": True}
FOLDERS = ("Finance leaders", "Food and retail")


def _day(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _fact(what, quote, days_ago, about, kind, source):
    return {"id": "f1", "what": what, "quote": quote, "date": _day(days_ago), "about": about, "kind": kind,
            "url": "https://example.com/sample", "source": source}


def _line(text, kind, why, new_paragraph=False, facts=()):
    return {"text": text, "kind": kind, "why": why, "new_paragraph": new_paragraph, "facts": list(facts)}


def _hook(summary, pressure, why, offering, score, **extra):
    return {"id": "f1", "fact_ids": ["f1"], "summary": summary, "pressure": pressure, "why_they_care": why,
            "offering_id": offering, "two_facts": False, "score": score, "parts": {}, **extra}


def _brief(tension, one_idea, proof, sticky, card, ask):
    return {"tension": tension, "one_idea": one_idea, "proof": proof, "sticky_line": sticky, "card_line": card, "ask": ask}


CARD_INVOICES = "Digital employees read, match and route invoices, and bring in people only when needed."

SAMPLES = [
    {
        "person": ("Priya Nair", "CFO", "Harbourline Logistics"),
        "folder": "Finance leaders",
        "pinned": True,
        "status": "ready",
        "fact": _fact("Priya said the team loses two days of every close to chasing invoice approvals.",
                      "we lose two days of every close just chasing approvals on invoices", 12, "person", "interview",
                      "Freight Finance Podcast"),
        "hook": _hook("Two days of every close go to chasing approvals", "The close waits on people reading their email.",
                      "As CFO, a late close is theirs to answer for.", "invoices", 88),
        "readers": {**GOOD_READERS, "remembered": "stop being a reminder service"},
        "subject": "Two days of chasing",
        "lines": [
            _line('You said your team loses "two days of every close just chasing approvals on invoices."', "fact", "Opens with their own words.", True, ["f1"]),
            _line("That is two days spent asking people to read their email.", "creative", "Makes the cost feel real."),
            _line("Nobody joins finance to become a very polite reminder service.", "creative", "The line meant to stick.", True),
            _line("Zamp's digital employees read each invoice, match it and send it to the right approver, then bring in a person only when something looks off.", "creative", "What we do, in plain words.", True),
            _line("Worth 15 minutes to see if those two days can come back?", "ask", "One small next step.", True),
        ],
        "brief": _brief("The close waits on inboxes.", "Your close shouldn't wait on people's inboxes.", "Their own words.",
                        "Stop being a reminder service.", CARD_INVOICES, "15 minutes"),
    },
    {
        "person": ("Tom Okafor", "VP Finance", "Brightloom Retail"),
        "folder": "Food and retail",
        "status": "approved",
        "fact": _fact("Brightloom is hiring three accounts payable specialists as stores open.",
                      "hiring three accounts payable specialists to keep up with new store openings", 9, "team", "hiring",
                      "Brightloom careers page"),
        "hook": _hook("Hiring three AP specialists to keep up with new stores", "Invoice work is growing with every store.",
                      "Each new store adds invoices to the same team.", "invoices", 81),
        "readers": {**GOOD_READERS, "remembered": "the next store shouldn't need another hire"},
        "subject": "Before the third hire",
        "lines": [
            _line("I saw Brightloom is hiring three accounts payable specialists to keep up with new stores.", "fact", "Shows why we're writing now.", True, ["f1"]),
            _line("It works, until the next store opens and the sums start again.", "creative", "Names the tension."),
            _line("Zamp's digital employees take on the reading, matching and routing, so invoice work can grow without the team growing with it.", "creative", "What we do, in plain words.", True),
            _line("Would 15 minutes before those roles are filled be useful?", "ask", "A reason to talk soon.", True),
        ],
        "brief": _brief("Hiring grows with every store.", "The next store shouldn't need another hire.", "Their open roles.",
                        "The next store shouldn't need a hire.", CARD_INVOICES, "15 minutes"),
    },
    {
        "person": ("Maya Lindqvist", "Head of Procurement", "Northfold Foods"),
        "folder": "Food and retail",
        "status": "ready",
        "fact": _fact("Maya wrote that purchase requests still arrive by email and spreadsheet.",
                      "most purchase requests still reach us by email and spreadsheet", 21, "person", "article",
                      "Food Supply Weekly"),
        "hook": _hook("Purchase requests still arrive by email and spreadsheet", "Requests get lost before anyone approves them.",
                      "Procurement gets blamed for delays it didn't cause.", "requisitions", 76),
        "readers": {**GOOD_READERS, "remembered": "a request shouldn't start in an inbox"},
        "subject": "Requests by spreadsheet",
        "lines": [
            _line('You wrote that "most purchase requests still reach us by email and spreadsheet."', "fact", "Opens with their own words.", True, ["f1"]),
            _line("So every order starts with someone working out what was actually asked for.", "creative", "Makes the cost feel real."),
            _line("Zamp's digital employees turn those messy requests into clean ones and send them on for approval.", "creative", "What we do, in plain words.", True),
            _line("Open to 15 minutes on what that could look like at Northfold?", "ask", "One small next step.", True),
        ],
        "brief": _brief("Requests start in inboxes.", "A request shouldn't start in an inbox.", "Their own words.",
                        "Requests shouldn't start in inboxes.", "Digital employees turn messy requests into clean ones and send them on for approval.", "15 minutes"),
    },
    {
        "person": ("Daniel Reyes", "Controller", "Cobalt Freight"),
        "status": "flagged",
        "fact": _fact("Cobalt Freight said it moved its finance team to a new system this year.",
                      "moved the whole finance team onto a new system this year", 40, "company", "news", "Cobalt Freight news"),
        "hook": _hook("The finance team moved to a new system this year", "A new system rarely fixes the manual steps around it.",
                      "The controller keeps the books right during the change.", "close", 63),
        "readers": {"only_them": False, "same_idea": True, "would_open": True, "kind": True, "remembered": "new system, same manual steps"},
        "problems": ["It could fit other people too. Add something only they would recognise."],
        "subject": "After the new system",
        "lines": [
            _line("I saw Cobalt Freight moved the whole finance team onto a new system this year.", "fact", "Shows why we're writing now.", True, ["f1"]),
            _line("New systems tend to leave the awkward manual steps exactly where they were.", "creative", "Names the tension."),
            _line("Zamp's digital employees pick up that repeat work around the close, and bring in people only when needed.", "creative", "What we do, in plain words.", True),
            _line("Worth 15 minutes to compare notes?", "ask", "One small next step.", True),
        ],
        "brief": _brief("Manual steps survive new systems.", "A new system doesn't remove the manual steps.", "Their news.",
                        "New system. Same manual steps.", "Digital employees pick up the repeat work around your close.", "15 minutes"),
    },
    {
        "person": ("Ines Moreau", "Director of Finance Operations", "Tessel Health"),
        "folder": "Finance leaders",
        "status": "ready",
        "fact": _fact("Tessel Health opened two new clinics this autumn.",
                      "opened two new clinics this autumn", 30, "company", "news", "Tessel Health press"),
        "hook": _hook("Tessel Health opened two new clinics", "More clinics may mean more invoices.",
                      "This is a guess, because we found no sign of invoice trouble.", "invoices", 48, weak=True),
        "readers": {**GOOD_READERS, "remembered": "more clinics, more paperwork"},
        "subject": "Two new clinics",
        "lines": [
            _line("Congratulations on opening two new clinics this autumn.", "fact", "A real and recent reason to write.", True, ["f1"]),
            _line("More clinics usually mean more suppliers and more invoices.", "creative", "An honest guess at the link."),
            _line("I can't tell from the outside whether that is a headache at Tessel, so this may not apply.", "creative", "Admits the link is weak.", True),
            _line("If it is, would 15 minutes on how Zamp's digital employees handle invoices be worth it?", "ask", "One small next step.", True),
        ],
        "brief": _brief("Growth adds paperwork.", "More clinics shouldn't mean more paperwork.", "Their news.",
                        "More clinics. Same paperwork team.", CARD_INVOICES, "15 minutes"),
    },
    {
        "person": ("Arjun Mehta", "CFO", "Quietwater Ltd"),
        "status": "ready",
        "fact": None,
        "readers": {"only_them": False, "same_idea": True, "would_open": False, "kind": True, "remembered": "good people on repeat work"},
        "subject": "Repeat work in finance",
        "lines": [
            _line("Most finance leaders I talk to have talented people spending their days on repeat work.", "creative", "A general truth about the job.", True),
            _line("Zamp's digital employees take on the reading, matching and routing, and bring in people only when needed.", "creative", "What we do, in plain words.", True),
            _line("Would 15 minutes be useful to see if any of that applies at Quietwater?", "ask", "One small next step.", True),
        ],
        "brief": _brief("Good people, repeat work.", "Good people shouldn't do repeat work.", "None.",
                        "Good people. Better work.", CARD_INVOICES, "15 minutes"),
    },
]


def _add(sample: dict, position: int, folder_ids: dict, seller: dict, user_id: str) -> None:
    name, title, company = sample["person"]
    prospect = {"name": name, "title": title, "company": company, "linkedin": "", "email": ""}
    created = time.time() - (position + 1) * DAY
    run_id = db.create_run(prospect, folder_ids.get(sample.get("folder")), created_at=created, user_id=user_id)

    hook = sample.get("hook") or scoring.general_hook(pipeline.role_focus(title, seller), seller["offerings"][0]["id"])
    problems = sample.get("problems", [])
    body = checks.compose_body(sample["lines"], name.split()[0], seller)
    draft = {"subject": sample["subject"], "lines": sample["lines"], "brief": sample["brief"], "body": body, "art": _art().get(sample["subject"])}
    final = None
    if sample["status"] == "approved":
        final = {"subject": sample["subject"], "body": body, "edited": False,
                 "card_headline": sample["brief"]["sticky_line"], "card_line": sample["brief"]["card_line"]}

    for key, _label in pipeline.STAGES:
        pipeline.stage(run_id, key, "done")
    pipeline.note(run_id, "choose", f"Best opening: {hook['summary']}")

    first, last = name.lower().split()
    db.update_run(
        run_id,
        status=sample["status"],
        sample=True,
        email={"address": f"{first}.{last}@example.com", "from": "found", "score": 90, "sources": []},
        facts=[sample["fact"]] if sample["fact"] else [],
        hooks=[hook],
        set_aside=[],
        hook_pos=0,
        draft=draft,
        seller_name=seller["seller"],
        confidence=scoring.confidence(hook, sample["readers"], True, bool(problems), False),
        tests=sample["readers"],
        problems=problems,
        rules_ok=True,
        **({"final": final} if final else {}),
    )
    if sample.get("pinned"):
        db.set_pinned(run_id, True)


def _art() -> dict:
    return json.loads(ART_FILE.read_text(encoding="utf-8")) if ART_FILE.exists() else {}


def add_missing_art(user: dict) -> None:
    """Test drafts made before cards had pictures."""
    pictures = _art()
    for run in db.list_runs(user["id"]):
        draft = run["data"].get("draft")
        if run["data"].get("sample") and draft and not draft.get("art") and draft["subject"] in pictures:
            db.update_run(run["id"], draft={**draft, "art": pictures[draft["subject"]]})


def seed(user: dict) -> None:
    seller = {**config.load_seller(), "sender": user["sender"] or user["name"]}
    folder_ids = {name: db.create_folder(name, user["id"]) for name in FOLDERS}
    for position, sample in enumerate(SAMPLES):
        _add(sample, position, folder_ids, seller, user["id"])
