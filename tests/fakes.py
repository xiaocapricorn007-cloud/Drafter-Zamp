"""Stand-ins for the outside services, for tests and for previewing the screens.

Nothing here is imported by the app. Real runs never touch this file.
"""
import json
import time

from app.gmail import GmailError
from app.providers import AgentTurn, ProviderError

QUOTE = "Our month-end close still runs on heroics and very long nights for the team"
JOBS = "We are hiring four accounts payable analysts to support invoice volume"
CUTS = "The company announced layoffs affecting two hundred staff across its offices"


def page(url, title, body, published=""):
    return {"title": title, "url": url, "text": body, "published": published}


class Fake:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []
        self.fail_search = self.fail_research = self.fail_gemini = False
        self.submissions = []  # scripted drafts for the writer
        self.snippets = []  # scripted Serper results for the questions
        self.profile = []  # what Google shows for an exact LinkedIn link
        self.no_products = False
        self.scenario = None

    def _wait(self):
        if self.delay:
            time.sleep(self.delay)

    def _which(self, text):
        if self.scenario:
            return self.scenario
        for key, word in (("common", "Sam Lee"), ("moved", "Old Co"), ("sensitive", "Cutter"), ("quiet", "Quiet"), ("weak", "Faraway")):
            if word in text:
                return key
        return "happy"

    def search_answers(self, query, news=False):
        self.calls.append(("search_answers", query))
        self._wait()
        if self.fail_search:
            raise ProviderError("search down")
        return self.snippets

    def web_search(self, query):
        self.calls.append(("search", query))
        self._wait()
        if self.fail_search:
            raise ProviderError("search down")
        if "linkedin.com/in/" in query:
            return self.profile
        return [
            {"title": "Profile one", "url": "https://example.com/one", "snippet": "A finance leader."},
            {"title": "Profile two", "url": "https://linkedin.com/in/asha-rao", "snippet": "CFO."},
        ]

    def research(self, query, topic="general"):
        self.calls.append(("research", query))
        self._wait()
        if self.fail_research:
            raise ProviderError("research down")
        which = self._which(query)
        company = query.split('"')[3] if query.count('"') >= 4 else query.split('"')[1]
        if which == "quiet":
            return []
        body = f"{company} and Rao. {QUOTE}. {JOBS}. {CUTS}."
        return [page(f"https://example.com/{abs(hash(query)) % 999}", "Finance podcast", body, "")]

    def find_email(self, first, last, company):
        self.calls.append(("hunter", first))
        self._wait()
        return {"email": f"{first.lower()}@example.com", "score": 91, "sources": ["https://example.com/one"]}

    def light_json(self, purpose, system, user, schema):
        self.calls.append(("light", purpose))
        self._wait()
        return getattr(self, f"_{purpose}")(user)

    def check_json(self, purpose, system, user, schema):
        return self.light_json(purpose, system, user, schema)

    def deep_json(self, purpose, system, user, schema):
        self.calls.append(("deep", purpose))
        self._wait()
        return getattr(self, f"_{purpose}")(user)

    def _people(self, user):
        which = self._which(user)
        typed = user.split("\n")[0].rsplit(", ", 1)[-1]
        one = {"name": "Asha Rao", "title": "CFO", "company": typed, "about": f"Runs finance at {typed}.", "source_ids": [0, 1]}
        if which == "common":
            return {"people": [
                {**one, "name": "Sam Lee", "company": "Northwind"},
                {"name": "Sam Lee", "title": "Designer", "company": "Studio Nine", "about": "A designer in Leeds.", "source_ids": [0]},
            ]}
        if which == "moved":
            return {"people": [{**one, "company": "Newco"}]}
        return {"people": [one]}

    def _questions(self, user):
        person = json.loads(user)["person"]
        ask = "month-end close heroics hiring accounts payable analysts invoice staff layoffs offices"
        return {"questions": [{"ask": f"{ask} {n}", "query": f'"{person["name"]}" "{person["company"]}" close', "news": False} for n in range(2)]}

    def _plan_write(self, user):
        packet = json.loads(user)
        facts = packet["facts"]
        which = self._which(" ".join(f["quote"] for f in facts) + packet["to"]["company"])
        weak = which == "weak"
        picks = sorted(facts, key=lambda f: "heroics" not in f["quote"])[:3]
        openings = [{
            "fact_id": f["id"], "offering_id": "none" if weak else "close", "summary": f["quote"][:60],
            "pressure": "The close depends on people working late.", "why_they_care": "As CFO this is theirs to fix.",
            "personal": 5, "pain": 5, "fit": 5, "unexpected": 4, "sensitive": False, "sensitive_reason": "",
        } for f in picks]
        return {"openings": openings, **self._next_draft(0, picks[0]["id"] if picks else None, picks[0]["quote"] if picks else "")}

    def _writer(self, user):
        packet = json.loads(user)
        first = (packet["opening"]["fact_ids"] or [None])[0]
        what = next((f["quote"] for f in packet["facts"] if f["id"] == first), "")
        return self._next_draft(1 if "problems" in packet else 0, first, what)

    def _next_draft(self, tries, fact_id, what):
        if self.submissions:
            return self.submissions[min(tries, len(self.submissions) - 1)]
        if fact_id is None:
            return self.general_draft()
        draft = self.good_draft(fact_id)
        if "hiring" in what:
            draft["lines"][0]["text"] = "Your team is hiring four accounts payable analysts."
        return draft

    def _reader(self, user):
        picked = "A"
        for line in user.splitlines():
            if "heroics" in line and line[:2] in {"A:", "B:", "C:", "D:", "E:"}:
                picked = line[0]
        return {"picked": picked, "sure": "high", "remembered": "stop hiring heroes for the close"}

    def _card_art(self, user):
        if self.fail_gemini:
            raise ProviderError("no art today")
        shapes = "".join(f'<circle cx="{120 + i * 80}" cy="300" r="40" fill="#f2e9da"/>' for i in range(4))
        return {"svg": f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 628">{shapes}</svg>', "shows": "four circles"}

    def _products(self, user):
        if self.no_products:
            return {"products": []}
        return {"products": [
            {"product": "Widget Cloud", "company": "Acme", "about": "Cloud software for closing the books.", "source_ids": [0]},
            {"product": "Widget Cloud", "company": "Other Co", "about": "A garden tool.", "source_ids": [1]},
        ]}

    def _product_questions(self, user):
        about = json.loads(user)
        ask = "month-end close heroics hiring accounts payable analysts invoice"
        return {"questions": [{"ask": f"{ask} {n}", "query": f'"{about["product"]}" "{about["company"]}" customers', "news": False} for n in range(3)]}

    def _product_profile(self, user):
        facts = json.loads(user)["facts"]
        hiring = [f["id"] for f in facts if "four" in f["quote"]]
        focus = {"cares_about": ["Speed"], "lead_with": "speed", "ask": "a 15-minute call"}
        return {
            "about": "Acme sells Widget Cloud, which closes the books faster.",
            "offerings": [{
                "name": "Faster close", "plain": "Takes on the repeated month-end work.", "problems": ["Slow close"],
                "buying_signals": ["Hiring accountants"], "search_terms": ["month-end close"],
                "proof": [
                    {"claim": "Northwind hired four analysts before switching.", "fact_ids": hiring},
                    {"claim": "Saved 90% of the cost at Globex.", "fact_ids": hiring},
                    {"claim": "Won a big award.", "fact_ids": ["nope"]},
                ],
            }],
            "role_focus": [{"match": ["CFO"], **focus}],
            "default_role_focus": focus,
        }

    def _judge(self, user):
        return {"same_idea": True, "would_open": True, "kind": True, "adds_outside_fact": False, "note": ""}

    def good_draft(self, fact_id="f1"):
        return {
            "brief": {"tension": "Close runs on heroics.", "one_idea": "Stop hiring heroes.", "proof": "Their own words.", "sticky_line": "Stop hiring heroes for month-end.", "card_line": "We take on the repeat work, so your close stops depending on late nights.", "ask": "15 minutes"},
            "subject": "month-end heroics",
            "lines": [
                {"text": 'You said your close "runs on heroics".', "kind": "fact", "facts": [fact_id], "why": "Opens with their own words.", "new_paragraph": True},
                {"text": "Adding people to that is hiring more heroes.", "kind": "creative", "facts": [], "why": "The line that sticks.", "new_paragraph": False},
                {"text": "We take on the repeat work, so the close stops depending on who stays late.", "kind": "creative", "facts": [], "why": "What we do, in plain terms.", "new_paragraph": True},
                {"text": "Worth 15 minutes to see if that fits?", "kind": "ask", "facts": [], "why": "One small next step.", "new_paragraph": True},
            ],
        }

    def general_draft(self):
        draft = self.good_draft()
        draft["lines"][0] = {"text": "Most finance leaders we talk to want the close to stop depending on late nights.", "kind": "creative", "facts": [], "why": "A general truth about the job.", "new_paragraph": True}
        return draft



class FakeMailer:
    def __init__(self):
        self.sent = []
        self.error = None

    def connect(self, code):
        return {"email": "me@gmail.com", "token": "sealed"}

    def disconnect(self, connection):
        pass

    def send(self, connection, from_name, outgoing, card):
        if self.error:
            raise self.error
        self.sent.append({"from": connection["email"], "name": from_name, "card": card, **outgoing})
        return f"msg{len(self.sent)}"


def ready_to_pitch(name):
    """Skip the first-time product setup, for tests that are about something else."""
    from app import config, db

    db.set_seller(db.find_user(name)["id"], json.dumps(config.load_seller()))


__all__ = ["Fake", "FakeMailer", "GmailError", "ready_to_pitch"]
