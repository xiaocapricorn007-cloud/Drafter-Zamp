"""SQLite storage: people, their drafts, the event log behind the live view, and saved lookups."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config

_path = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL,
    prospect TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq);
CREATE TABLE IF NOT EXISTS saved (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    locker TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""

# Added after the first version, so older databases get them when the app starts.
RUN_COLUMNS = {
    "folder_id": "TEXT",
    "pinned": "INTEGER NOT NULL DEFAULT 0",
    "locked": "INTEGER NOT NULL DEFAULT 0",
    "user_id": "TEXT",
    "title": "TEXT",
    "sealed": "TEXT",
}
FOLDER_COLUMNS = {"user_id": "TEXT"}
USER_COLUMNS = {"gmail": "TEXT", "seller": "TEXT", "setup": "TEXT"}

BUSY = ("finding", "working")
IN_FLIGHT = BUSY + ("sending",)  # a job holds this row, so it can't be removed
UNLOCKABLE = IN_FLIGHT + ("scheduled",)  # the app needs to read these to finish them
LOOKUP_KINDS = ("people_results", "email", "pages")


@contextmanager
def _conn():
    conn = sqlite3.connect(_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete=ON")  # removed text is wiped, not just marked as free
    try:
        yield conn
    finally:
        conn.close()


def _add_columns(c, table: str, columns: dict) -> list[str]:
    have = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
    added = [name for name in columns if name not in have]
    for name in added:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {columns[name]}")
    return added


def init(path: str | None = None) -> None:
    global _path
    if path:
        _path = path
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        added = _add_columns(c, "runs", RUN_COLUMNS)
        _add_columns(c, "folders", FOLDER_COLUMNS)
        _add_columns(c, "users", USER_COLUMNS)
        if "sealed" in added:
            c.execute("VACUUM")  # older versions left deleted text lying in unused space
        # Work that was running when the app stopped can never finish.
        marks = ",".join("?" for _ in BUSY)
        interrupted = {"problem": "The app restarted while this was running. Please start again."}
        for row in c.execute(f"SELECT id, data FROM runs WHERE status IN ({marks})", BUSY).fetchall():
            data = {**json.loads(row["data"]), **interrupted}
            c.execute(
                "UPDATE runs SET status='failed', data=?, updated_at=? WHERE id=?",
                (json.dumps(data), time.time(), row["id"]),
            )
        for row in c.execute("SELECT id, setup FROM users WHERE setup IS NOT NULL").fetchall():
            setup = json.loads(row["setup"])
            if setup.get("status") in ("finding", "researching"):
                setup.update(status="failed", problem="The app restarted while this was running. Please try again.")
                c.execute("UPDATE users SET setup=? WHERE id=?", (json.dumps(setup), row["id"]))
        # It may or may not have reached Gmail, so it must never be sent again by itself.
        unsure = "The app restarted while this was sending. Please check your Sent folder before trying again."
        for row in c.execute("SELECT id, data FROM runs WHERE status='sending'").fetchall():
            data = json.loads(row["data"])
            status, fields = back_from_outbox(data, unsure)
            c.execute(
                "UPDATE runs SET status=?, data=?, updated_at=? WHERE id=?",
                (status, json.dumps({**data, **fields}), time.time(), row["id"]),
            )


def back_from_outbox(data: dict, problem: str | None) -> tuple[str, dict]:
    """A cancelled or failed send returns to the draft screen, keeping any edits."""
    outbox = data.get("outbox") or {}
    draft = data["draft"]
    if outbox:
        brief = {**draft["brief"], "sticky_line": outbox["card_headline"]}
        draft = {**draft, "subject": outbox["subject"], "body": outbox["body"], "brief": brief}
    return outbox.get("was", "ready"), {"draft": draft, "outbox": None, "send_problem": problem, "to_override": outbox.get("to")}


# ---- people ---------------------------------------------------------------


def create_user(name: str, password: str, sender: str = "") -> str | None:
    """Returns the new id, or None when the name is already taken."""
    user_id = uuid.uuid4().hex[:12]
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO users (id, name, password, sender, created_at) VALUES (?,?,?,?,?)",
                (user_id, name, password, sender, time.time()),
            )
    except sqlite3.IntegrityError:
        return None
    return user_id


def get_user(user_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def find_user(name: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def update_profile(user_id: str, name: str, sender: str) -> bool:
    try:
        with _conn() as c:
            c.execute("UPDATE users SET name=?, sender=? WHERE id=?", (name, sender, user_id))
    except sqlite3.IntegrityError:
        return False
    return True


def set_password(user_id: str, password: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET password=? WHERE id=?", (password, user_id))


def set_locker(user_id: str, locker: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET locker=? WHERE id=?", (locker, user_id))


def set_seller(user_id: str, seller: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET seller=? WHERE id=?", (seller, user_id))


def get_setup(user_id: str) -> dict:
    with _conn() as c:
        row = c.execute("SELECT setup FROM users WHERE id=?", (user_id,)).fetchone()
    return json.loads(row["setup"]) if row and row["setup"] else {}


def set_setup(user_id: str, setup: dict | None) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET setup=? WHERE id=?", (json.dumps(setup) if setup is not None else None, user_id))


def update_setup(user_id: str, **fields) -> None:
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT setup FROM users WHERE id=?", (user_id,)).fetchone()
        c.execute("UPDATE users SET setup=? WHERE id=?", (json.dumps({**json.loads(row["setup"] or "{}"), **fields}), user_id))
        c.execute("COMMIT")


def move_setup(user_id: str, was: str, to: str, **fields) -> bool:
    """Only one answer can win, so a double click can't start the research twice."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT setup FROM users WHERE id=?", (user_id,)).fetchone()
        setup = json.loads(row["setup"] or "{}")
        if setup.get("status") != was:
            c.execute("ROLLBACK")
            return False
        c.execute("UPDATE users SET setup=? WHERE id=?", (json.dumps({**setup, **fields, "status": to}), user_id))
        c.execute("COMMIT")
    return True


def set_gmail(user_id: str, connection: str | None) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET gmail=? WHERE id=?", (connection, user_id))


def claim_unowned(user_id: str) -> int:
    """Drafts made before accounts existed go to the first person who signs up."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        claimed = c.execute("UPDATE runs SET user_id=? WHERE user_id IS NULL", (user_id,)).rowcount
        c.execute("UPDATE folders SET user_id=? WHERE user_id IS NULL", (user_id,))
        c.execute("COMMIT")
    return claimed


def add_session(token_hash: str, user_id: str, expires_at: float) -> None:
    with _conn() as c:
        c.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash, user_id, expires_at))
        c.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))


def session_user(token_hash: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires_at>?",
            (token_hash, time.time()),
        ).fetchone()
    return dict(row) if row else None


def drop_session(token_hash: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))


def drop_other_sessions(user_id: str, keep_hash: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, keep_hash))


# ---- drafts ---------------------------------------------------------------


def create_run(prospect: dict, folder_id: str | None = None, created_at: float | None = None, user_id: str | None = None) -> str:
    run_id = uuid.uuid4().hex[:12]
    now = created_at or time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO runs (id, created_at, updated_at, status, prospect, data, folder_id, user_id) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, now, now, "finding", json.dumps(prospect), "{}", folder_id, user_id),
        )
    return run_id


def _row(row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "prospect": json.loads(row["prospect"]),
        "data": json.loads(row["data"]),
        "folder_id": row["folder_id"],
        "pinned": bool(row["pinned"]),
        "locked": bool(row["locked"]),
        "user_id": row["user_id"],
        "title": row["title"],
        "sealed": row["sealed"],
    }


def get_run(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _row(row) if row else None


def list_runs(user_id: str, limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)
        ).fetchall()
    return [_row(r) for r in rows]


def scheduled_runs() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM runs WHERE status='scheduled'").fetchall()
    return [_row(r) for r in rows]


def update_run(run_id: str, status: str | None = None, prospect: dict | None = None, **data) -> None:
    """Merge fields into a run in one transaction, so threads cannot overwrite each other."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status, prospect, data FROM runs WHERE id=?", (run_id,)).fetchone()
        merged = {**json.loads(row["data"]), **data}
        c.execute(
            "UPDATE runs SET status=?, prospect=?, data=?, updated_at=? WHERE id=?",
            (
                status or row["status"],
                json.dumps(prospect) if prospect is not None else row["prospect"],
                json.dumps(merged),
                time.time(),
                run_id,
            ),
        )
        c.execute("COMMIT")
    if status:
        add_event(run_id, "status", {"status": status})


def move_status(run_id: str, allowed: tuple[str, ...], to: str) -> bool:
    """Change status only if it is currently allowed. Stops double clicks starting work twice."""
    marks = ",".join("?" for _ in allowed)
    with _conn() as c:
        cur = c.execute(
            f"UPDATE runs SET status=?, updated_at=? WHERE id=? AND locked=0 AND status IN ({marks})",
            (to, time.time(), run_id, *allowed),
        )
        changed = cur.rowcount == 1
    if changed:
        add_event(run_id, "status", {"status": to})
    return changed


def set_pinned(run_id: str, pinned: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE runs SET pinned=? WHERE id=?", (int(pinned), run_id))


def set_title(run_id: str, title: str) -> None:
    with _conn() as c:
        c.execute("UPDATE runs SET title=? WHERE id=?", (title, run_id))


def set_art(run_id: str, token: str, picture: str | None) -> bool:
    """The picture arrives after the email. By then the draft may be locked or replaced, so check first."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT data FROM runs WHERE id=? AND locked=0", (run_id,)).fetchone()
        data = json.loads(row["data"]) if row else {}
        draft = data.get("draft") or {}
        if draft.get("token") != token:
            c.execute("ROLLBACK")
            return False
        data["draft"] = {**draft, "art": picture}
        c.execute("UPDATE runs SET data=?, updated_at=? WHERE id=?", (json.dumps(data), time.time(), run_id))
        c.execute("COMMIT")
    return True


def is_locked(run_id: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT locked FROM runs WHERE id=?", (run_id,)).fetchone()
    return bool(row and row["locked"])


def count_locked(user_id: str) -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM runs WHERE user_id=? AND locked=1", (user_id,)).fetchone()[0]


def seal_run(run_id: str, encrypt) -> bool:
    """Swap a draft's readable text for its encrypted form. Never while a job still needs to read it."""
    marks = ",".join("?" for _ in UNLOCKABLE)
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            f"SELECT * FROM runs WHERE id=? AND locked=0 AND status NOT IN ({marks})", (run_id, *UNLOCKABLE)
        ).fetchone()
        if row is None:
            c.execute("ROLLBACK")
            return False
        events = c.execute("SELECT ts, kind, payload FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        sealed = encrypt({
            "prospect": json.loads(row["prospect"]),
            "data": json.loads(row["data"]),
            "title": row["title"],
            "events": [dict(e) for e in events],
        })
        c.execute("UPDATE runs SET prospect='{}', data='{}', title=NULL, sealed=?, locked=1 WHERE id=?", (sealed, run_id))
        c.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        c.execute("COMMIT")
    _flush()
    return True


def unseal_run(run_id: str, opened: dict) -> None:
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "UPDATE runs SET prospect=?, data=?, title=?, sealed=NULL, locked=0 WHERE id=?",
            (json.dumps(opened["prospect"]), json.dumps(opened["data"]), opened["title"], run_id),
        )
        c.executemany(
            "INSERT INTO events (run_id, ts, kind, payload) VALUES (?,?,?,?)",
            [(run_id, e["ts"], e["kind"], e["payload"]) for e in opened["events"]],
        )
        c.execute("COMMIT")


def _flush() -> None:
    """Move finished writes out of the side file, so old text doesn't linger in it."""
    with _conn() as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def forget_lookups(keys: list[str]) -> None:
    """Saved web results name the person, so a locked draft's must not stay readable."""
    if not keys:
        return
    kinds = ",".join("?" for _ in LOOKUP_KINDS)
    names = ",".join("?" for _ in keys)
    with _conn() as c:
        c.execute(f"DELETE FROM saved WHERE kind IN ({kinds}) AND key IN ({names})", (*LOOKUP_KINDS, *[k.lower() for k in keys]))
    _flush()


def move_to_folder(run_id: str, folder_id: str | None) -> None:
    with _conn() as c:
        c.execute("UPDATE runs SET folder_id=? WHERE id=?", (folder_id, run_id))


def delete_run(run_id: str) -> bool:
    """Finished work only. A running job would crash if its row vanished."""
    marks = ",".join("?" for _ in IN_FLIGHT)
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(f"DELETE FROM runs WHERE id=? AND status NOT IN ({marks})", (run_id, *IN_FLIGHT))
        deleted = cur.rowcount == 1
        if deleted:
            c.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        c.execute("COMMIT")
    return deleted


# ---- folders --------------------------------------------------------------


def list_folders(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id, name FROM folders WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def folder_exists(folder_id: str, user_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM folders WHERE id=? AND user_id=?", (folder_id, user_id)).fetchone() is not None


def folder_name_taken(name: str, user_id: str, ignore_id: str | None = None) -> bool:
    return any(f["name"].lower() == name.lower() and f["id"] != ignore_id for f in list_folders(user_id))


def create_folder(name: str, user_id: str) -> str:
    folder_id = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO folders (id, name, created_at, user_id) VALUES (?,?,?,?)", (folder_id, name, time.time(), user_id))
    return folder_id


def rename_folder(folder_id: str, name: str) -> None:
    with _conn() as c:
        c.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))


def delete_folder(folder_id: str) -> None:
    """The drafts inside are kept. They just stop being in a folder."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE runs SET folder_id=NULL WHERE folder_id=?", (folder_id,))
        c.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        c.execute("COMMIT")


# ---- the live event log and saved lookups ---------------------------------


def add_event(run_id: str, kind: str, payload: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO events (run_id, ts, kind, payload) VALUES (?,?,?,?)",
            (run_id, time.time(), kind, json.dumps(payload)),
        )


def events_since(run_id: str, seq: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, seq)
        ).fetchall()
    return [
        {"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], **json.loads(r["payload"])} for r in rows
    ]


def save(kind: str, key: str, payload) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO saved VALUES (?,?,?,?)",
            (kind, key.lower(), json.dumps(payload), time.time()),
        )


def load(kind: str, key: str) -> tuple[object, float] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT payload, created_at FROM saved WHERE kind=? AND key=?", (kind, key.lower())
        ).fetchone()
    return (json.loads(row["payload"]), row["created_at"]) if row else None
