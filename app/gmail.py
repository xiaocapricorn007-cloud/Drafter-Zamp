"""Sending from the person's own Gmail, through Google's Gmail API.

The app asks only for permission to send, so it can never read anyone's inbox.
Sending is never retried automatically: a second try could deliver the same email twice.
"""
import base64
import hashlib
import html
import json
import os
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

SCOPES = "https://www.googleapis.com/auth/gmail.send openid email"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
TIMEOUT = 60


class GmailError(Exception):
    def __init__(self, message: str, reconnect: bool = False):
        super().__init__(message)
        self.message = message
        self.reconnect = reconnect


def configured() -> bool:
    return all(config.env(k) for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_KEY"))


def redirect_uri() -> str:
    return config.env("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/api/gmail/callback")


def _cipher() -> AESGCM:
    return AESGCM(hashlib.sha256(config.env("TOKEN_KEY").encode()).digest())


def seal_token(token: str) -> str:
    nonce = os.urandom(12)
    return base64.b64encode(nonce + _cipher().encrypt(nonce, token.encode(), b"gmail-token")).decode()


def open_token(sealed: str) -> str:
    raw = base64.b64decode(sealed)
    try:
        return _cipher().decrypt(raw[:12], raw[12:], b"gmail-token").decode()
    except InvalidTag:
        raise GmailError("Your Gmail connection can't be read any more. Please connect it again in Settings.", reconnect=True) from None


def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": config.env("GOOGLE_CLIENT_ID"),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def build_message(from_name: str, from_email: str, outgoing: dict, card: bytes | None) -> bytes:
    """A normal email with a plain version, a formatted version, and the card shown inside it."""
    message = EmailMessage()
    message["From"] = formataddr((from_name, from_email))
    message["To"] = outgoing["to"]
    message["Subject"] = outgoing["subject"]
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=from_email.rsplit("@", 1)[-1])
    message.set_content(outgoing["body"])

    paragraphs = "".join(
        f'<p style="margin:0 0 14px">{html.escape(part).replace(chr(10), "<br>")}</p>'
        for part in outgoing["body"].split("\n\n") if part.strip()
    )
    picture = ""
    cid = make_msgid(domain="drafter.card")
    if card:
        alt = html.escape(outgoing.get("card_headline", ""), quote=True)
        picture = f'<img src="cid:{cid[1:-1]}" alt="{alt}" width="560" style="display:block;max-width:100%;height:auto;border-radius:8px">'
    message.add_alternative(
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;color:#222">{paragraphs}{picture}</div>',
        subtype="html",
    )
    if card:
        message.get_payload()[1].add_related(card, "image", "png", cid=cid, filename="card.png")
    return message.as_bytes()


class Mailer:
    def _token_call(self, **fields) -> httpx.Response:
        fields |= {"client_id": config.env("GOOGLE_CLIENT_ID"), "client_secret": config.env("GOOGLE_CLIENT_SECRET")}
        try:
            return httpx.post(TOKEN_URL, data=fields, timeout=TIMEOUT)
        except httpx.HTTPError:
            raise GmailError("We couldn't reach Google just now. Please try again.") from None

    def connect(self, code: str) -> dict:
        reply = self._token_call(code=code, grant_type="authorization_code", redirect_uri=redirect_uri())
        if reply.status_code != 200:
            raise GmailError("Google didn't accept that sign-in.")
        tokens = reply.json()
        if SEND_SCOPE not in tokens.get("scope", "") or not tokens.get("refresh_token"):
            raise GmailError("Please allow Drafter to send email when Google asks.")
        try:
            who = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=TIMEOUT)
        except httpx.HTTPError:
            raise GmailError("We couldn't reach Google just now. Please try again.") from None
        if who.status_code != 200 or not who.json().get("email"):
            raise GmailError("Google didn't tell us which account this is.")
        return {"email": who.json()["email"], "token": seal_token(tokens["refresh_token"])}

    def disconnect(self, connection: dict) -> None:
        """Best effort: the local connection is removed either way."""
        try:
            httpx.post(REVOKE_URL, data={"token": open_token(connection["token"])}, timeout=TIMEOUT)
        except (httpx.HTTPError, GmailError):
            pass

    def _access_token(self, connection: dict) -> str:
        reply = self._token_call(refresh_token=open_token(connection["token"]), grant_type="refresh_token")
        if reply.status_code == 200:
            return reply.json()["access_token"]
        if reply.status_code in (400, 401) and reply.json().get("error") == "invalid_grant":
            raise GmailError("Your Gmail connection has ended. Please connect it again in Settings.", reconnect=True)
        raise GmailError("Google wouldn't let us send just now. Please try again.")

    def send(self, connection: dict, from_name: str, outgoing: dict, card: bytes | None) -> str:
        token = self._access_token(connection)
        raw = base64.urlsafe_b64encode(build_message(from_name, connection["email"], outgoing, card)).decode()
        try:
            reply = httpx.post(SEND_URL, headers={"Authorization": f"Bearer {token}"}, json={"raw": raw}, timeout=TIMEOUT)
        except httpx.HTTPError:
            raise GmailError("We couldn't tell if this went out. Please check your Sent folder before trying again.") from None
        if reply.status_code == 200:
            return reply.json().get("id", "")
        if reply.status_code in (401, 403):
            raise GmailError("Gmail refused this. Please connect it again in Settings.", reconnect=reply.status_code == 401)
        if reply.status_code == 429:
            raise GmailError("Gmail says you've sent too much for now. Please try later.")
        if reply.status_code == 400:
            raise GmailError("Gmail couldn't accept this email. Please check the address.")
        raise GmailError("Gmail had a problem and didn't send this. Please try again.")
