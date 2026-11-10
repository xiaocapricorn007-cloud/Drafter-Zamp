"""Reading a LinkedIn link through Google's public listing of it. LinkedIn itself is never opened."""
import re

from . import verify

_LINKEDIN_END = re.compile(r"\s*[|\-–—]\s*LinkedIn\s*$", re.I)
_DASH = re.compile(r"\s+[-–—]\s+")


def parse_title(title: str) -> dict:
    """Google shows these as 'Name - Job - Company | LinkedIn' or 'Name - Job at Company'."""
    parts = [p.strip(" .…") for p in _DASH.split(_LINKEDIN_END.sub("", title or "").strip())]
    parts = [p for p in parts if p]
    name = parts[0] if parts else ""
    job = parts[1] if len(parts) > 1 else ""
    company = parts[2] if len(parts) > 2 else ""
    if not company and " at " in job:
        job, company = (p.strip() for p in job.rsplit(" at ", 1))
    return {"name": name, "job": job, "company": company}


def names_match(typed: str, shown: str) -> bool:
    """First names must agree. A last name may be shortened to an initial, as people do on LinkedIn."""
    a, b = (re.findall(r"[^\W\d_]+", verify.normalise(n)) for n in (typed, shown))
    if not a or not b or a[0] != b[0]:
        return False
    if len(a) == 1 or len(b) == 1:
        return True
    return a[-1].startswith(b[-1]) or b[-1].startswith(a[-1])


def look_up(providers, slug: str) -> dict | None:
    """Returns what Google shows for this exact profile, or None if Google hasn't listed it."""
    title, url, snippets = "", "", []
    for query in (f"site:linkedin.com/in/{slug}", f'"linkedin.com/in/{slug}"'):
        for result in providers.web_search(query):
            if verify.linkedin_slug(result["url"]) != slug:
                continue
            title, url = title or result["title"], url or result["url"]
            if result["snippet"] and result["snippet"] not in snippets:
                snippets.append(result["snippet"])
    return {"title": title, "url": url, "snippets": snippets, **parse_title(title)} if url else None


def fits(found: dict, prospect: dict) -> tuple[bool, bool]:
    """Does this profile show the name and the company that were typed?"""
    text = verify.normalise(" ".join([found["title"], *found["snippets"]]))
    core = verify.company_core(prospect["company"])
    return names_match(prospect["name"], found["name"]), bool(core) and (core in text or verify.same_company(found["company"], prospect["company"]))
