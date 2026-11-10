"""Getting approved emails out, now or later.

Gmail's API has no way to send later, so Drafter holds a scheduled email and sends it on time.
Every email is claimed by one atomic status change first, so it can only ever go out once.
"""
import base64
import binascii
import json
import logging
import time

from . import db
from .gmail import GmailError

log = logging.getLogger("drafter")

MAX_AHEAD = 90 * 86400
MAX_CARD_BYTES = 4 * 1024 * 1024
LATE_LIMIT = 3600  # an email meant for 9am shouldn't surprise anyone at midnight
PNG_START = b"\x89PNG\r\n\x1a\n"


def decode_card(encoded: str) -> bytes | None:
    """An empty card is fine. Anything that isn't really a small PNG raises ValueError."""
    if not encoded:
        return None
    try:
        card = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("That card couldn't be read.") from None
    if not card.startswith(PNG_START) or len(card) > MAX_CARD_BYTES:
        raise ValueError("That card couldn't be read.")
    return card


def _put_back(run_id: str, problem: str | None) -> None:
    run = db.get_run(run_id)
    status, fields = db.back_from_outbox(run["data"], problem)
    db.update_run(run_id, status=status, **fields)


def cancel(run_id: str) -> bool:
    if not db.move_status(run_id, ("scheduled",), "working"):
        return False
    _put_back(run_id, None)
    return True


def deliver(run_id: str, mailer) -> None:
    """The run must already be in the 'sending' status, which only one caller can win."""
    run = db.get_run(run_id)
    outgoing = run["data"]["outbox"]
    user = db.get_user(run["user_id"])
    try:
        if not user["gmail"]:
            raise GmailError("Your Gmail isn't connected. Please connect it in Settings.")
        connection = json.loads(user["gmail"])
        message_id = mailer.send(connection, user["sender"] or user["name"], outgoing, decode_card(outgoing.get("card_png", "")))
    except GmailError as exc:
        if exc.reconnect:
            db.set_gmail(user["id"], None)
        _put_back(run_id, exc.message)
        return
    except Exception:
        log.exception("Sending %s failed", run_id)
        _put_back(run_id, "We couldn't tell if this went out. Please check your Sent folder before trying again.")
        return
    db.update_run(
        run_id,
        status="sent",
        outbox=None,
        send_problem=None,
        final={"subject": outgoing["subject"], "body": outgoing["body"], "edited": True,
               "card_headline": outgoing["card_headline"], "card_line": run["data"]["draft"]["brief"].get("card_line", "")},
        sent={"to": outgoing["to"], "from": connection["email"], "at": time.time(), "message_id": message_id},
    )


def run_due(mailer, now: float | None = None) -> None:
    now = now or time.time()
    for run in db.scheduled_runs():
        send_at = (run["data"].get("outbox") or {}).get("send_at")
        if send_at is None or send_at > now:
            continue
        if not db.move_status(run["id"], ("scheduled",), "sending"):
            continue  # someone cancelled it, or another worker got here first
        if now - send_at > LATE_LIMIT:
            _put_back(run["id"], "This didn't go out, because Drafter wasn't running at the time you picked.")
            continue
        deliver(run["id"], mailer)
