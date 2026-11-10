"""Sending and scheduling through Gmail, and the drawn card. No real email ever leaves these tests."""
import base64
import email
import email.policy
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import art, db, gmail, main, outbox, pipeline
from tests.fakes import Fake, FakeMailer, ready_to_pitch

PERSON = {"name": "Asha Rao", "title": "CFO", "company": "Northwind"}
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"pretend picture").decode()
MESSAGE = {"to": "buyer@northwind.co", "subject": "Hello there", "body": "Hi Asha,\n\nWorth a chat?\n\nBest,\nRep", "card_headline": "Big words", "card_png": PNG}


@pytest.fixture
def setup(tmp_path):
    db.init(str(tmp_path / "t.db"))
    pipeline.run_inline = True
    fake, mailer = Fake(), FakeMailer()
    main.app.state.providers, main.app.state.mailer = fake, mailer
    client = TestClient(main.app)
    client.post("/api/signup", json={"name": "Rep One", "password": "a long password"})
    ready_to_pitch("Rep One")
    yield client, fake, mailer
    main.app.state.providers = main.app.state.mailer = None
    pipeline.run_inline = False


def connect():
    db.set_gmail(db.find_user("Rep One")["id"], json.dumps({"email": "me@gmail.com", "token": "sealed"}))


def draft(client):
    run_id = client.post("/api/runs", json=PERSON).json()["id"]
    client.post(f"/api/runs/{run_id}/confirm", json={"person": 0})
    return run_id


def get(client, run_id):
    return client.get(f"/api/runs/{run_id}").json()


def send(client, run_id, **extra):
    return client.post(f"/api/runs/{run_id}/send", json={**MESSAGE, **extra})


# ---- the drawn card --------------------------------------------------------

GOOD_ART = '<svg viewBox="0 0 480 628">' + '<circle cx="50" cy="50" r="20" fill="#f2e9da"/>' * 4 + "</svg>"


def test_a_clean_drawing_is_kept():
    cleaned = art.clean_svg(GOOD_ART)
    assert cleaned and cleaned.count("<circle") == 4 and 'viewBox="0 0 480 628"' in cleaned


@pytest.mark.parametrize("bad", [
    GOOD_ART.replace("</svg>", "<script>alert(1)</script></svg>"),
    GOOD_ART.replace("</svg>", '<image href="https://evil.test/x.png"/></svg>'),
    GOOD_ART.replace("</svg>", "<text>hello</text></svg>"),
    GOOD_ART.replace("</svg>", '<foreignObject width="9" height="9"/></svg>'),
    '<!DOCTYPE svg [<!ENTITY a "b">]>' + GOOD_ART,
    "not a drawing at all",
    '<svg viewBox="0 0 480 628"><circle r="4"/></svg>',
    '<html><circle/><circle/><circle/><circle/></html>',
    "",
])
def test_unsafe_or_empty_drawings_are_refused(bad):
    assert art.clean_svg(bad) is None


def test_bad_attributes_and_colours_do_not_survive():
    dirty = GOOD_ART.replace('fill="#f2e9da"', 'fill="url(#x)" onload="alert(1)" href="https://evil.test" stroke="#00ff00" opacity="0.5"')
    cleaned = art.clean_svg(dirty)
    assert cleaned and "onload" not in cleaned and "evil" not in cleaned and "url(" not in cleaned and "#00ff00" not in cleaned
    assert 'opacity="0.5"' in cleaned


def test_a_new_draft_comes_with_a_drawing(setup):
    client, _, _ = setup
    run_id = draft(client)
    run = get(client, run_id)
    assert "<circle" in run["data"]["draft"]["art"]
    assert {s["key"]: s["state"] for s in run["timeline"]}["art"] == "done"


def test_a_failed_drawing_never_stops_the_draft(setup):
    client, fake, _ = setup
    fake.fail_gemini = True
    run = get(client, draft(client))
    assert run["status"] == "ready" and run["data"]["draft"]["art"] is None


# ---- sending ---------------------------------------------------------------


def test_sending_needs_a_connected_gmail(setup):
    client, _, mailer = setup
    run_id = draft(client)
    assert send(client, run_id).status_code == 409
    assert mailer.sent == [] and get(client, run_id)["status"] == "ready"


def test_send_now_delivers_once_and_records_it(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    assert send(client, run_id).status_code == 200
    run = get(client, run_id)
    assert run["status"] == "sent" and run["data"]["sent"]["to"] == "buyer@northwind.co" and run["data"]["sent"]["message_id"] == "msg1"
    assert run["data"]["final"]["subject"] == "Hello there" and run["data"]["outbox"] is None
    assert len(mailer.sent) == 1 and mailer.sent[0]["name"] == "Rep One" and mailer.sent[0]["card"].startswith(b"\x89PNG")
    assert send(client, run_id).status_code == 409
    assert len(mailer.sent) == 1


@pytest.mark.parametrize("field,value", [
    ("to", "not an address"), ("to", "a@b.co, c@d.co"), ("to", "someone@example.com"), ("to", ""),
    ("subject", ""), ("subject", "two\nlines"), ("body", "   "),
])
def test_bad_emails_are_refused(setup, field, value):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    assert send(client, run_id, **{field: value}).status_code == 422
    assert mailer.sent == []


@pytest.mark.parametrize("card", ["not base64!!", base64.b64encode(b"<svg>not a png</svg>").decode()])
def test_only_real_pngs_can_be_attached(setup, card):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    assert send(client, run_id, card_png=card).status_code == 400
    assert mailer.sent == []


def test_failed_send_returns_to_the_draft_with_edits_and_the_reason(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    mailer.error = gmail.GmailError("Gmail says no.")
    send(client, run_id, subject="My edit")
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["send_problem"] == "Gmail says no."
    assert run["data"]["draft"]["subject"] == "My edit" and run["data"]["to_override"] == "buyer@northwind.co"
    mailer.error = None
    send(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "sent" and run["data"]["send_problem"] is None


def test_an_ended_connection_is_cleared(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    mailer.error = gmail.GmailError("Please connect again.", reconnect=True)
    send(client, run_id)
    assert client.get("/api/me").json()["gmail"] is None


def test_unexpected_crash_says_to_check_sent_folder(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    mailer.error = RuntimeError("boom")
    send(client, run_id)
    assert "Sent folder" in get(client, run_id)["data"]["send_problem"]


def test_finished_without_sending_can_still_be_sent(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    client.post(f"/api/runs/{run_id}/review", json={"action": "approve", "subject": "s", "body": "b", "card_headline": "c"})
    assert send(client, run_id).status_code == 200
    assert get(client, run_id)["status"] == "sent" and len(mailer.sent) == 1


def test_a_locked_or_someone_elses_draft_cannot_be_sent(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    other = TestClient(main.app)
    other.post("/api/signup", json={"name": "Rep Two", "password": "a long password"})
    assert send(other, run_id).status_code == 404
    client.post(f"/api/runs/{run_id}/lock", json={"code": "long-code-1234"})
    assert send(client, run_id).status_code == 423
    assert mailer.sent == []


def test_app_restart_while_sending_never_resends(tmp_path, setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    db.update_run(run_id, outbox={**MESSAGE, "send_at": None, "was": "ready"})
    db.move_status(run_id, ("ready",), "sending")
    db.init(str(tmp_path / "t.db"))
    run = get(client, run_id)
    assert run["status"] == "ready" and "Sent folder" in run["data"]["send_problem"] and mailer.sent == []


def test_work_being_sent_cannot_be_deleted(setup):
    client, _, _ = setup
    run_id = draft(client)
    db.move_status(run_id, ("ready",), "sending")
    assert client.delete(f"/api/runs/{run_id}").status_code == 409


# ---- scheduling ------------------------------------------------------------


def test_schedule_waits_then_sends_exactly_once(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    when = time.time() + 3600
    assert send(client, run_id, send_at=when).status_code == 200
    run = get(client, run_id)
    assert run["status"] == "scheduled" and run["data"]["outbox"]["send_at"] == when
    assert "card_png" not in run["data"]["outbox"]

    outbox.run_due(mailer, when - 60)
    assert mailer.sent == [] and get(client, run_id)["status"] == "scheduled"
    outbox.run_due(mailer, when + 1)
    assert len(mailer.sent) == 1 and get(client, run_id)["status"] == "sent"
    outbox.run_due(mailer, when + 20)
    assert len(mailer.sent) == 1


@pytest.mark.parametrize("offset", [-60, 0, 91 * 86400])
def test_schedule_time_must_be_sensible(setup, offset):
    client, _, _ = setup
    connect()
    run_id = draft(client)
    assert send(client, run_id, send_at=time.time() + offset).status_code == 400
    assert get(client, run_id)["status"] == "ready"


def test_too_late_is_not_sent_and_says_so(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    when = time.time() + 3600
    send(client, run_id, send_at=when)
    outbox.run_due(mailer, when + outbox.LATE_LIMIT + 5)
    run = get(client, run_id)
    assert mailer.sent == [] and run["status"] == "ready" and "wasn't running" in run["data"]["send_problem"]


def test_cancel_returns_to_editing(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    when = time.time() + 3600
    send(client, run_id, send_at=when, subject="Edited subject")
    assert client.post(f"/api/runs/{run_id}/cancel-send", json={}).status_code == 200
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["draft"]["subject"] == "Edited subject" and run["data"]["send_problem"] is None
    outbox.run_due(mailer, when + 1)
    assert mailer.sent == []
    assert client.post(f"/api/runs/{run_id}/cancel-send", json={}).status_code == 409


def test_scheduled_email_cannot_be_locked(setup):
    client, _, _ = setup
    connect()
    run_id = draft(client)
    send(client, run_id, send_at=time.time() + 3600)
    assert client.post(f"/api/runs/{run_id}/lock", json={"code": "long-code-1234"}).status_code == 409
    assert db.get_run(run_id)["locked"] is False


def test_deleting_a_scheduled_email_stops_it(setup):
    client, _, mailer = setup
    connect()
    run_id = draft(client)
    when = time.time() + 3600
    send(client, run_id, send_at=when)
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    outbox.run_due(mailer, when + 1)
    assert mailer.sent == []


# ---- the Gmail connection and message ---------------------------------------


def test_the_email_itself_is_well_formed():
    raw = gmail.build_message("Rep One", "me@gmail.com", {"to": "a@b.co", "subject": "Héllo", "body": "Hi <b>you</b>,\n\nSecond part.", "card_headline": 'Big "words"'}, b"\x89PNGdata")
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert parsed["To"] == "a@b.co" and parsed["Subject"] == "Héllo" and "Rep One" in parsed["From"]
    assert "Hi <b>you</b>" in parsed.get_body(("plain",)).get_content()
    page = parsed.get_body(("html",)).get_content()
    assert "&lt;b&gt;" in page and "<b>you" not in page and 'alt="Big &quot;words&quot;"' in page
    picture = next(p for p in parsed.walk() if p.get_content_type() == "image/png")
    assert picture.get_content() == b"\x89PNGdata" and f'cid:{picture["Content-ID"][1:-1]}' in page


def test_gmail_token_is_stored_encrypted(monkeypatch):
    monkeypatch.setenv("TOKEN_KEY", "a test key")
    sealed = gmail.seal_token("my-refresh-token")
    assert "my-refresh-token" not in sealed and gmail.open_token(sealed) == "my-refresh-token"
    monkeypatch.setenv("TOKEN_KEY", "a different key")
    with pytest.raises(gmail.GmailError):
        gmail.open_token(sealed)


def test_connect_is_only_offered_when_set_up(setup, monkeypatch):
    client, _, _ = setup
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert client.get("/api/me").json()["gmail_ready"] is False
    assert client.get("/api/gmail/connect", follow_redirects=False).status_code == 409


def enable_google(monkeypatch):
    for key, value in (("GOOGLE_CLIENT_ID", "cid"), ("GOOGLE_CLIENT_SECRET", "secret"), ("TOKEN_KEY", "key")):
        monkeypatch.setenv(key, value)


def test_connect_flow_stores_the_account(setup, monkeypatch):
    client, _, _ = setup
    enable_google(monkeypatch)
    go = client.get("/api/gmail/connect", follow_redirects=False)
    assert go.status_code == 303 and "accounts.google.com" in go.headers["location"] and "gmail.send" in go.headers["location"]
    assert "mail.google.com" not in go.headers["location"] and "gmail.readonly" not in go.headers["location"]
    state = client.cookies.get(main.GMAIL_STATE_COOKIE)
    back = client.get(f"/api/gmail/callback?code=abc&state={state}", follow_redirects=False)
    assert back.status_code == 303 and "gmail=connected" in back.headers["location"]
    assert client.get("/api/me").json()["gmail"] == "me@gmail.com"
    client.post("/api/gmail/disconnect", json={})
    assert client.get("/api/me").json()["gmail"] is None


def test_a_forged_return_from_google_is_refused(setup, monkeypatch):
    client, _, _ = setup
    enable_google(monkeypatch)
    client.get("/api/gmail/connect", follow_redirects=False)
    back = client.get("/api/gmail/callback?code=abc&state=forged", follow_redirects=False)
    assert "gmail=failed" in back.headers["location"]
    assert client.get("/api/me").json()["gmail"] is None
