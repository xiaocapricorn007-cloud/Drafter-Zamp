"""Turns search results into facts the writer can use. All plain code, so it costs no AI time.

A fact's quote is always text that really appeared in the result, which is what lets the
later checks prove every claim in the email.
"""
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from . import scoring, verify

MAX_FACTS = 36
MAX_PER_PAGE = 3
MAX_PER_SITE = 5
MIN_CHARS, MAX_CHARS = 40, 420
ENOUGH_POINTS = 3

_STOP = {"about", "after", "their", "there", "these", "those", "which", "where", "what", "when", "with", "have", "from", "that", "this", "does", "they", "them", "been", "will", "would", "could", "said", "into", "more", "most", "some", "than", "very"}
_JUNK = re.compile(r"cookie|subscribe|sign in|sign up|log in|click here|all rights reserved|privacy policy|terms of (use|service)|newsletter", re.I)
_SPLIT = re.compile(r"(?<=[.!?])[\"”']?\s+(?=[A-Z\"“])")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_AGO = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}
_QUOTES = re.compile(r"[\"“”]")
_PRONOUN = re.compile(r"(he|she|they|his|her|their)\b", re.I)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MAX_FROM_OFFICIAL_PAGE = 14
MIN_OFFICIAL_CHARS = 25


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP}


def normal_date(text: str, today: date) -> str:
    """Search engines say '2 weeks ago' or 'Sep 23, 2026'. The scoring needs a real date."""
    text = (text or "").strip()
    ago = _AGO.search(text)
    if ago:
        return (today - timedelta(days=int(ago.group(1)) * _DAYS[ago.group(2).lower()])).isoformat()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    parsed = scoring.parse_date(text)
    return parsed.isoformat() if parsed else ""


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(" ".join(text.split())) if s.strip()]


def _usable(text: str) -> bool:
    return MIN_CHARS <= len(text) <= MAX_CHARS and not _JUNK.search(text) and text.count("|") < 2


def _snippet_part(snippet: str) -> str:
    """Google joins separate bits of a page with '...'. Words across a gap were never said together."""
    parts = [p.strip() for p in _ELLIPSIS.split(snippet) if p.strip()]
    return max(parts, key=len, default="")


def _points(text: str, last: str, company: str, asked: set[str]) -> int:
    norm = verify.normalise(text)
    points = (3 if last in norm else 0) + (2 if company in norm else 0)
    points += min(3, len(_words(text) & asked))
    return points + (2 if _QUOTES.search(text) else 0)


def _official_lines(text: str) -> list[str]:
    cleaned = (_LINK.sub(r"\1", _IMAGE.sub("", line)).strip(" #*>-\t") for line in text.split("\n"))
    return [line for line in cleaned if line]


def _official_passages(hit: dict, asked: set[str]) -> list[dict]:
    """Company websites use cards and short lines, not sentences. A quote's author is on the next line."""
    lines = _official_lines(hit["text"])
    found = []
    for i, line in enumerate(lines):
        for part in sentences(line) if len(line) > MAX_CHARS else [line]:
            if MIN_OFFICIAL_CHARS <= len(part) <= MAX_CHARS and not _JUNK.search(part):
                near = " / ".join(x for x in (lines[i - 1] if i else "", *lines[i + 1 : i + 3]) if len(x) < 80)
                points = min(3, len(_words(part) & asked)) + (3 if re.search(r"\d", part) else 0) + (3 if _QUOTES.search(part) else 0)
                found.append({"text": part, "context": near, "points": points})
    found.sort(key=lambda p: -p["points"])
    return found[:MAX_FROM_OFFICIAL_PAGE]


def _passages(hit: dict, last: str, company: str, asked: set[str]) -> list[dict]:
    if hit["kind"] == "page" and hit.get("trusted"):
        return _official_passages(hit, asked)
    if hit["kind"] == "snippet":
        part = _snippet_part(hit["text"])
        return [{"text": part, "context": "", "points": _points(part, last, company, asked)}] if _usable(part) else []
    found = []
    parts = sentences(hit["text"])
    for i, sentence in enumerate(parts):
        if not _usable(sentence):
            continue
        points = _points(sentence, last, company, asked)
        if i and _PRONOUN.match(sentence) and last in verify.normalise(parts[i - 1]):
            points += 3  # "She said..." straight after a sentence naming them is about them
        if points >= ENOUGH_POINTS:
            found.append({"text": sentence, "context": parts[i - 1][:MAX_CHARS] if i else "", "points": points})
    found.sort(key=lambda p: -p["points"])
    return found[:MAX_PER_PAGE]


def build(hits: list[dict], prospect: dict, today: date, strict: bool = False, avoid_touchy: bool = True, limit: int = MAX_FACTS) -> tuple[list[dict], list[dict]]:
    """Returns (the best facts, best first; the touchy ones we refused to pass on).

    With strict on, a page must name their company. It's for people known by an exact profile,
    who may share their name with someone better known.
    """
    last = verify.normalise(prospect["name"].split()[-1])
    company = verify.company_core(prospect["company"])
    candidates, left_out, seen = [], [], set()
    for hit in hits:
        whole = verify.normalise(f"{hit['title']} {hit['text']}")
        if not hit.get("trusted") and company not in whole and (strict or last not in whole):
            continue  # someone else with a similar name
        for passage in _passages(hit, last, company, _words(hit["ask"])):
            norm = verify.normalise(passage["text"])
            if norm in seen:
                continue  # the same story copied onto another site is not a new fact
            seen.add(norm)
            # A fraud-detection product is allowed to say "fraud". A person's history isn't.
            reason = scoring.is_sensitive({"what": passage["text"], "quote": hit["title"]}, {}) if avoid_touchy else ""
            if reason:
                left_out.append({"summary": passage["text"], "reason": reason, "score": 0, "sensitive": True})
                continue
            names_them = hit.get("trusted") or last in verify.normalise(f"{passage['context']} {passage['text']}")
            when = normal_date(hit.get("date", ""), today)
            recent = bool(when) and scoring.freshness(when, today) >= 7
            candidates.append({
                "what": passage["text"],
                "quote": passage["text"],
                "context": passage["context"],
                "date": when,
                "about": "person" if names_them else "company",
                "kind": hit["kind"],
                "url": hit["url"],
                "source": hit["title"],
                "answers": hit["ask"],
                "_rank": passage["points"] + (1 if recent else 0),
            })
    candidates.sort(key=lambda f: -f["_rank"])
    facts, per_site = [], {}
    for fact in candidates:
        site = urlparse(fact["url"]).netloc
        if per_site.get(site, 0) >= MAX_PER_SITE:
            continue
        per_site[site] = per_site.get(site, 0) + 1
        facts.append({"id": f"f{len(facts) + 1}", **{k: v for k, v in fact.items() if k != "_rank"}})
        if len(facts) >= limit:
            break
    return facts, left_out
