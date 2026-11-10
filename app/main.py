"""The web app: pages, the API, and the live stream of progress."""
import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from . import accounts, config, db, gmail, lock, outbox, pipeline, product, samples
from .providers import Providers

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would print API keys
STATIC = config.ROOT / "static"
PRIVATE_MESSAGE = "This draft is private. Please unlock it first."
STILL_WRITING = "This one is still being written. Please wait for it to finish."
CANNOT_LOCK_SCHEDULED = "This one is scheduled to send. Please cancel that first."
SESSION_COOKIE = "drafter_session"
GMAIL_STATE_COOKIE = "drafter_gmail_state"
CLOCK_SECONDS = 15
log = logging.getLogger("drafter")


@asynccontextmanager
async def lifespan(_app):
    db.init()
    clock = asyncio.create_task(_send_scheduled())
    yield
    clock.cancel()


app = FastAPI(title="Drafter", lifespan=lifespan)
app.state.providers = None
app.state.mailer = None


async def _send_scheduled() -> None:
    while True:
        await asyncio.sleep(CLOCK_SECONDS)
        try:
            await asyncio.to_thread(outbox.run_due, mailer())
        except Exception:
            log.exception("The send clock had a problem")


def mailer():
    if app.state.mailer is None:
        app.state.mailer = gmail.Mailer()
    return app.state.mailer


def providers():
    if app.state.providers is None:
        app.state.providers = Providers()
    return app.state.providers


class NewRun(BaseModel):
    name: str
    title: str
    company: str
    linkedin: str = ""
    email: str = ""

    @field_validator("name", "title", "company", "linkedin", "email")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("name", "title", "company")
    @classmethod
    def required(cls, value: str) -> str:
        if not value:
            raise ValueError("Please fill this in.")
        return value

    @field_validator("linkedin")
    @classmethod
    def linkedin_ok(cls, value: str) -> str:
        if value and "linkedin.com" not in value.lower():
            raise ValueError("That doesn't look like a LinkedIn link.")
        return value

    @field_validator("email")
    @classmethod
    def email_ok(cls, value: str) -> str:
        if value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("That doesn't look like an email.")
        return value


class Confirm(BaseModel):
    person: int | None = None  # None means "none of these"


class Review(BaseModel):
    action: str
    subject: str = ""
    body: str = ""
    card_headline: str = ""


class Send(BaseModel):
    to: str
    subject: str
    body: str
    card_headline: str = ""
    card_png: str = ""
    send_at: float | None = None

    @field_validator("to")
    @classmethod
    def to_ok(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+", value):
            raise ValueError("Please add one real email address.")
        if value.lower().endswith(("@example.com", "@example.org", "@example.net", ".test", ".invalid")):
            raise ValueError("That's a made-up test address. Please type a real one.")
        return value

    @field_validator("subject")
    @classmethod
    def subject_ok(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 200 or "\n" in value or "\r" in value:
            raise ValueError("Please write a one-line subject.")
        return value

    @field_validator("body")
    @classmethod
    def body_ok(cls, value: str) -> str:
        if not value.strip() or len(value) > 20_000:
            raise ValueError("The email is empty or too long.")
        return value

    @field_validator("card_headline")
    @classmethod
    def headline_ok(cls, value: str) -> str:
        return " ".join(value.split())[:120]


class Code(BaseModel):
    code: str = ""


class Credentials(BaseModel):
    name: str
    password: str

    @field_validator("name")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()


class Profile(BaseModel):
    name: str
    sender: str = ""

    @field_validator("name", "sender")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("sender")
    @classmethod
    def sender_ok(cls, value: str) -> str:
        if len(value) > 60:
            raise ValueError("Please keep the name under 60 letters.")
        return value


class NewSecret(BaseModel):
    current: str = ""
    new: str


class NewProduct(BaseModel):
    company: str
    product: str
    website: str = ""

    @field_validator("company", "product", "website")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("company", "product")
    @classmethod
    def required(cls, value: str) -> str:
        if not 2 <= len(value) <= 80:
            raise ValueError("Please fill this in.")
        return value


class ProductChoice(BaseModel):
    choice: int | None = None  # None means "none of these"
    description: str = ""


class Rename(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def title_ok(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Please give the draft a name.")
        if len(value) > 80:
            raise ValueError("Please keep the name under 80 letters.")
        return value


class Pin(BaseModel):
    pinned: bool


class Move(BaseModel):
    folder_id: str | None = None


class FolderName(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_ok(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Please give the folder a name.")
        if len(value) > 40:
            raise ValueError("Please keep the name under 40 letters.")
        return value


def current_user(drafter_session: Annotated[str | None, Cookie()] = None) -> dict:
    user = db.session_user(accounts.hash_token(drafter_session)) if drafter_session else None
    if user is None:
        raise HTTPException(401, "Please log in.")
    return user


User = Annotated[dict, Depends(current_user)]


def _view(run: dict) -> dict:
    shown = {k: v for k, v in run.items() if k not in ("sealed", "user_id")}
    if shown["data"].get("outbox"):  # the picture is large and the page draws its own
        waiting = {k: v for k, v in shown["data"]["outbox"].items() if k != "card_png"}
        shown["data"] = {**shown["data"], "outbox": waiting}
    return {**shown, "timeline": pipeline.timeline(run["id"])}


def _get(run_id: str, user: dict) -> dict:
    """Someone else's draft looks exactly like one that doesn't exist."""
    run = db.get_run(run_id)
    if not run or run["user_id"] != user["id"]:
        raise HTTPException(404, "We can't find that one.")
    return run


def _open(run_id: str, user: dict) -> dict:
    """For anything that shows a draft or changes what's in it."""
    run = _get(run_id, user)
    if run["locked"]:
        raise HTTPException(423, PRIVATE_MESSAGE)
    return run


def _start(details: dict, user: dict, folder_id: str | None = None) -> str:
    if config.missing_keys() and app.state.providers is None:
        raise HTTPException(503, "This app still needs its setup keys.")
    if not user["seller"]:
        raise HTTPException(409, "Please tell us what you're selling first.")
    run_id = db.create_run(details, folder_id, user_id=user["id"])
    db.update_run(run_id, seller=json.loads(user["seller"]))  # later product changes leave this draft alone
    pipeline.launch(pipeline.find_person, run_id, providers())
    return run_id


def _folder_or_404(folder_id: str, user: dict) -> None:
    if not db.folder_exists(folder_id, user["id"]):
        raise HTTPException(404, "We can't find that folder.")


def _name_is_free(name: str, user: dict, ignore_id: str | None = None) -> None:
    if db.folder_name_taken(name, user["id"], ignore_id):
        raise HTTPException(409, "You already have a folder with that name.")


def _not_too_many(key: str) -> None:
    try:
        accounts.tries.check(key)
    except accounts.TooManyTries:
        raise HTTPException(429, "Too many wrong tries. Please wait 30 seconds.") from None


def _key_from_code(user: dict, code: str) -> bytes:
    guard = f"code:{user['id']}"
    _not_too_many(guard)
    key = lock.open_locker(user["locker"], code)
    if key is None:
        accounts.tries.failed(guard)
        raise HTTPException(403, "That code isn't right.")
    accounts.tries.passed(guard)
    return key


def _check_new_code(code: str) -> None:
    if len(code) < lock.MIN_LENGTH:
        raise HTTPException(400, f"Please use at least {lock.MIN_LENGTH} characters.")


def _check_new_name(name: str) -> None:
    if not 2 <= len(name) <= 40:
        raise HTTPException(400, "Please use a name between 2 and 40 letters.")


def _check_new_password(password: str) -> None:
    if len(password) < accounts.MIN_PASSWORD:
        raise HTTPException(400, f"Please use a password of at least {accounts.MIN_PASSWORD} characters.")


def _pitching(user: dict) -> dict | None:
    if not user["seller"]:
        return None
    seller = json.loads(user["seller"])
    return {
        "company": seller["seller"], "product": seller.get("product", ""), "about": seller["about"],
        "offerings": [{"name": o["name"], "plain": o["plain"], "proof": o["proof"]} for o in seller["offerings"]],
    }


def _about(user: dict) -> dict:
    return {
        "pitching": _pitching(user),
        "name": user["name"],
        "sender": user["sender"],
        "has_code": bool(user["locker"]),
        "has_locked": db.count_locked(user["id"]) > 0,
        "gmail": json.loads(user["gmail"])["email"] if user["gmail"] else None,
        "gmail_ready": gmail.configured(),
    }


def _sign_in(request: Request, response: Response, user_id: str) -> None:
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        SESSION_COOKIE, accounts.new_session(user_id), max_age=accounts.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=https, path="/",
    )


@app.get("/api/status")
def status():
    return {"missing": config.missing_keys()}


@app.post("/api/signup")
def signup(body: Credentials, request: Request, response: Response):
    _check_new_name(body.name)
    _check_new_password(body.password)
    user_id = db.create_user(body.name, accounts.hash_password(body.password))
    if user_id is None:
        raise HTTPException(409, "That name is taken. Please try another.")
    user = db.get_user(user_id)
    if db.claim_unowned(user_id):
        pipeline.sign_old_drafts(user)
        samples.add_missing_art(user)
    else:
        samples.seed(user)
    _sign_in(request, response, user_id)
    return _about(user)


@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response):
    guard = f"login:{body.name.lower()}"
    _not_too_many(guard)
    user = db.find_user(body.name)
    if not accounts.password_matches(user["password"] if user else None, body.password):
        accounts.tries.failed(guard)
        raise HTTPException(401, "That name or password isn't right.")
    accounts.tries.passed(guard)
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        db.drop_session(accounts.hash_token(old))
    _sign_in(request, response, user["id"])
    return _about(user)


@app.post("/api/logout")
def logout(request: Request, response: Response):
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        db.drop_session(accounts.hash_token(old))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: User):
    return _about(user)


@app.get("/api/setup")
def setup_status(user: User):
    return db.get_setup(user["id"]) or {"status": "none"}


@app.post("/api/setup")
def setup_begin(body: NewProduct, user: User):
    if db.get_setup(user["id"]).get("status") in product.BUSY:
        raise HTTPException(409, "We're already working on this.")
    if config.missing_keys() and app.state.providers is None:
        raise HTTPException(503, "This app still needs its setup keys.")
    product.begin(user["id"], providers(), body.company, body.product, body.website)
    return {"ok": True}


@app.post("/api/setup/confirm")
def setup_confirm(body: ProductChoice, user: User):
    try:
        product.choose(user["id"], providers(), body.choice, body.description)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except LookupError:
        raise HTTPException(409, "This has already been answered.") from None
    return {"ok": True}


@app.post("/api/setup/reset")
def setup_reset(user: User):
    if db.get_setup(user["id"]).get("status") in product.BUSY:
        raise HTTPException(409, "We're still working on this.")
    db.set_setup(user["id"], None)
    return {"ok": True}


@app.post("/api/settings/profile")
def save_profile(body: Profile, user: User):
    _check_new_name(body.name)
    if not db.update_profile(user["id"], body.name, body.sender):
        raise HTTPException(409, "That name is taken. Please try another.")
    return {"ok": True}


@app.post("/api/settings/password")
def change_password(body: NewSecret, user: User, drafter_session: Annotated[str | None, Cookie()] = None):
    guard = f"password:{user['id']}"
    _not_too_many(guard)
    if not accounts.password_matches(user["password"], body.current):
        accounts.tries.failed(guard)
        raise HTTPException(403, "Your current password isn't right.")
    accounts.tries.passed(guard)
    _check_new_password(body.new)
    db.set_password(user["id"], accounts.hash_password(body.new))
    db.drop_other_sessions(user["id"], accounts.hash_token(drafter_session))
    return {"ok": True}


@app.post("/api/settings/code")
def change_code(body: NewSecret, user: User):
    """With nothing locked there is nothing to protect, so a forgotten code can be replaced."""
    _check_new_code(body.new)
    if user["locker"] and db.count_locked(user["id"]):
        locker = lock.wrap_key(_key_from_code(user, body.current), body.new)
    else:
        locker, _ = lock.new_locker(body.new)
    db.set_locker(user["id"], locker)
    return {"ok": True}


@app.post("/api/runs")
def create(body: NewRun, user: User):
    return {"id": _start(body.model_dump(), user)}


@app.get("/api/runs")
def runs(user: User):
    items = []
    for run in db.list_runs(user["id"]):
        place = {"id": run["id"], "created_at": run["created_at"], "status": run["status"], "title": run["title"],
                 "folder_id": run["folder_id"], "pinned": run["pinned"], "locked": run["locked"]}
        if run["locked"]:  # nothing about who it's for leaves the server
            items.append({**place, "prospect": None, "opening": None, "subject": None, "confidence": None})
            continue
        data = run["data"]
        hook = None
        if data.get("hooks"):
            hook = data["hooks"][min(data.get("hook_pos", 0), len(data["hooks"]) - 1)]["summary"]
        items.append({
            **place,
            "prospect": run["prospect"],
            "opening": hook,
            "subject": (data.get("final") or data.get("draft") or {}).get("subject"),
            "confidence": (data.get("confidence") or {}).get("score"),
        })
    return items


@app.get("/api/runs/{run_id}")
def read(run_id: str, user: User):
    return _view(_open(run_id, user))


@app.get("/api/runs/{run_id}/events")
def events(run_id: str, user: User):
    _open(run_id, user)

    async def stream():
        seq, idle = 0, 0
        while True:
            fresh = await asyncio.to_thread(db.events_since, run_id, seq)
            if fresh and await asyncio.to_thread(db.is_locked, run_id):
                return  # it was locked while someone was watching
            for ev in fresh:
                seq = ev["seq"]
                yield f"data: {json.dumps(ev)}\n\n"
            idle = 0 if fresh else idle + 1
            if idle % 40 == 39:
                yield ": still here\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/runs/{run_id}/confirm")
def confirm(run_id: str, body: Confirm, user: User):
    run = _open(run_id, user)
    people = run["data"].get("people", [])
    if body.person is not None and not 0 <= body.person < len(people):
        raise HTTPException(400, "Please pick one of the people shown.")
    if not db.move_status(run_id, ("waiting",), "working"):
        raise HTTPException(409, "This has already been answered.")
    if body.person is None:
        # Pages about the wrong person can't be used, so write from what the rep typed.
        db.update_run(run_id, unconfirmed=True)
        pipeline.stage(run_id, "confirm", "done")
        pipeline.note(run_id, "confirm", "None of those were right, so we'll use what you gave us")
        pipeline.stage(run_id, "email", "skipped")
        pipeline.launch(pipeline.write_from_details, run_id, providers(), "We'll write from the details you gave us")
        return {"ok": True}
    chosen = people[body.person]
    if not chosen["sources"]:
        db.update_run(run_id, unconfirmed=True)
    prospect = {
        **run["prospect"],
        "name": chosen["name"] or run["prospect"]["name"],
        "title": chosen["title"] or run["prospect"]["title"],
        "company": chosen["company"] or run["prospect"]["company"],
    }
    db.update_run(run_id, prospect=prospect, confirmed=chosen)
    if chosen["different_company"]:
        pipeline.note(run_id, "confirm", f"Updated: they're now at {chosen['company']}")
    pipeline.stage(run_id, "confirm", "done")
    pipeline.launch(pipeline.research_and_write, run_id, providers())
    return {"ok": True}


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, body: Review, user: User):
    run = _open(run_id, user)
    data = run["data"]
    if body.action == "next":
        pos = data.get("hook_pos", 0) + 1
        if pos >= len(data.get("hooks", [])):
            raise HTTPException(409, "There are no other openings left.")
        if not db.move_status(run_id, ("ready", "flagged"), "working"):
            raise HTTPException(409, "This has already been answered.")
        db.update_run(run_id, hook_pos=pos)
        pipeline.launch_next_hook(run_id, providers(), pos)
    elif body.action == "discard":
        if not db.move_status(run_id, ("ready", "flagged"), "discarded"):
            raise HTTPException(409, "This has already been answered.")
    elif body.action == "approve":
        if not body.subject.strip() or not body.body.strip():
            raise HTTPException(400, "The email is empty.")
        if not db.move_status(run_id, ("ready", "flagged"), "approved"):
            raise HTTPException(409, "This has already been answered.")
        original = data["draft"]
        edited = body.subject != original["subject"] or body.body != original.get("body")
        db.update_run(
            run_id,
            final={
                "subject": body.subject,
                "body": body.body,
                "edited": edited,
                "card_headline": body.card_headline or original["brief"].get("sticky_line", ""),
                "card_line": original["brief"].get("card_line", ""),
            },
        )
    else:
        raise HTTPException(400, "Unknown action.")
    return {"ok": True}


@app.post("/api/runs/{run_id}/send")
def send(run_id: str, body: Send, user: User):
    """Now or later. Saved first and claimed second, so a double click can't send twice."""
    run = _open(run_id, user)
    if not user["gmail"]:
        raise HTTPException(409, "Please connect your Gmail in Settings first.")
    try:
        outbox.decode_card(body.card_png)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    now = time.time()
    if body.send_at is not None and not now + 30 <= body.send_at <= now + outbox.MAX_AHEAD:
        raise HTTPException(400, "Please pick a time in the future, within the next 90 days.")
    if run["status"] not in ("ready", "flagged", "approved"):
        raise HTTPException(409, "This has already been answered.")
    db.update_run(run_id, outbox={**body.model_dump(), "was": "flagged" if run["status"] == "flagged" else "ready"}, send_problem=None)
    target = "scheduled" if body.send_at is not None else "sending"
    if not db.move_status(run_id, ("ready", "flagged", "approved"), target):
        raise HTTPException(409, "This has already been answered.")
    if target == "sending":
        pipeline.launch(outbox.deliver, run_id, mailer())
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel-send")
def cancel_send(run_id: str, user: User):
    _open(run_id, user)
    if not outbox.cancel(run_id):
        raise HTTPException(409, "It's too late to cancel this one.")
    return {"ok": True}


@app.get("/api/gmail/connect")
def gmail_connect(request: Request, user: User):
    if not gmail.configured():
        raise HTTPException(409, "Gmail isn't set up on this app yet.")
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(gmail.auth_url(state), 303)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(GMAIL_STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax", secure=https, path="/")
    return response


@app.get("/api/gmail/callback")
def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    def finish(result: str):
        response = RedirectResponse(f"/?gmail={result}#/settings", 303)
        response.delete_cookie(GMAIL_STATE_COOKIE, path="/")
        return response

    session = request.cookies.get(SESSION_COOKIE)
    user = db.session_user(accounts.hash_token(session)) if session else None
    expected = request.cookies.get(GMAIL_STATE_COOKIE, "")
    if error or not code or user is None or not expected or not hmac.compare_digest(state, expected):
        return finish("failed")
    try:
        connection = mailer().connect(code)
    except gmail.GmailError as exc:
        log.warning("Gmail connect failed: %s", exc.message)
        return finish("failed")
    db.set_gmail(user["id"], json.dumps(connection))
    return finish("connected")


@app.post("/api/gmail/disconnect")
def gmail_disconnect(user: User):
    if user["gmail"]:
        mailer().disconnect(json.loads(user["gmail"]))
        db.set_gmail(user["id"], None)
    return {"ok": True}


@app.post("/api/runs/{run_id}/pin")
def pin(run_id: str, body: Pin, user: User):
    _get(run_id, user)
    db.set_pinned(run_id, body.pinned)
    return {"ok": True}


@app.post("/api/runs/{run_id}/rename")
def rename(run_id: str, body: Rename, user: User):
    _open(run_id, user)
    db.set_title(run_id, body.title)
    return {"ok": True}


@app.post("/api/runs/{run_id}/folder")
def move(run_id: str, body: Move, user: User):
    _get(run_id, user)
    if body.folder_id is not None:
        _folder_or_404(body.folder_id, user)
    db.move_to_folder(run_id, body.folder_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/lock")
def lock_run(run_id: str, body: Code, user: User):
    run = _open(run_id, user)
    if run["status"] == "scheduled":
        raise HTTPException(409, CANNOT_LOCK_SCHEDULED)
    if run["status"] in db.IN_FLIGHT:
        raise HTTPException(409, STILL_WRITING)
    if user["locker"]:
        key = _key_from_code(user, body.code)
    else:
        _check_new_code(body.code)
        locker, key = lock.new_locker(body.code)
        db.set_locker(user["id"], locker)
    keys = pipeline.lookup_keys(run)
    if not db.seal_run(run_id, lambda content: lock.seal(key, run_id, content)):
        raise HTTPException(409, STILL_WRITING)
    db.forget_lookups(keys)
    return {"ok": True}


@app.post("/api/runs/{run_id}/unlock")
def unlock_run(run_id: str, body: Code, user: User):
    run = _get(run_id, user)
    if not run["locked"]:
        return {"ok": True}
    key = _key_from_code(user, body.code)
    db.unseal_run(run_id, lock.unseal(key, run_id, run["sealed"]))
    return {"ok": True}


@app.post("/api/runs/{run_id}/redo")
def redo(run_id: str, user: User):
    run = _open(run_id, user)
    if run["status"] in db.IN_FLIGHT:
        raise HTTPException(409, STILL_WRITING)
    details = {key: run["prospect"].get(key, "") for key in NewRun.model_fields}
    return {"id": _start(details, user, run["folder_id"])}


@app.delete("/api/runs/{run_id}")
def delete(run_id: str, user: User):
    """A locked draft can go too, or a forgotten code would leave it stuck forever."""
    _get(run_id, user)
    if not db.delete_run(run_id):
        raise HTTPException(409, STILL_WRITING)
    return {"ok": True}


@app.get("/api/folders")
def folders(user: User):
    return db.list_folders(user["id"])


@app.post("/api/folders")
def new_folder(body: FolderName, user: User):
    _name_is_free(body.name, user)
    return {"id": db.create_folder(body.name, user["id"])}


@app.patch("/api/folders/{folder_id}")
def rename_folder(folder_id: str, body: FolderName, user: User):
    _folder_or_404(folder_id, user)
    _name_is_free(body.name, user, folder_id)
    db.rename_folder(folder_id, body.name)
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: str, user: User):
    _folder_or_404(folder_id, user)
    db.delete_folder(folder_id)
    return {"ok": True}


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
