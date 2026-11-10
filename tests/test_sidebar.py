"""The sidebar's folders, pins, locks, delete and redo, through the real API."""
import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient

from app import accounts, db, lock, main, pipeline, samples
from tests.fakes import Fake, ready_to_pitch

PERSON = {"name": "Asha Rao", "title": "CFO", "company": "Northwind"}
CODE = "long-code-4821"
PASSWORD = "a long password"


@pytest.fixture
def client(tmp_path):
    db.init(str(tmp_path / "t.db"))
    pipeline.run_inline = True
    main.app.state.providers = Fake()
    test_client = TestClient(main.app)
    assert test_client.post("/api/signup", json={"name": "Rep One", "password": PASSWORD}).status_code == 200
    ready_to_pitch("Rep One")
    yield test_client
    main.app.state.providers = None
    pipeline.run_inline = False


def finished_draft(client):
    run_id = client.post("/api/runs", json=PERSON).json()["id"]
    client.post(f"/api/runs/{run_id}/confirm", json={"person": 0})
    return run_id


def row(client, run_id):
    return next(r for r in client.get("/api/runs").json() if r["id"] == run_id)


def test_folders_can_be_made_renamed_and_removed_without_losing_drafts(client):
    run_id = finished_draft(client)
    folder = client.post("/api/folders", json={"name": "  Finance  "}).json()["id"]
    assert {"id": folder, "name": "Finance"} in client.get("/api/folders").json()
    assert client.post(f"/api/runs/{run_id}/folder", json={"folder_id": folder}).status_code == 200
    assert row(client, run_id)["folder_id"] == folder
    assert client.patch(f"/api/folders/{folder}", json={"name": "Money"}).status_code == 200
    assert client.delete(f"/api/folders/{folder}").status_code == 200
    assert row(client, run_id)["folder_id"] is None
    assert folder not in [f["id"] for f in client.get("/api/folders").json()]


def test_folder_names_must_be_real_and_different(client):
    client.post("/api/folders", json={"name": "Finance"})
    assert client.post("/api/folders", json={"name": "finance"}).status_code == 409
    assert client.post("/api/folders", json={"name": "   "}).status_code == 422


def test_cannot_move_into_a_folder_that_does_not_exist(client):
    run_id = finished_draft(client)
    assert client.post(f"/api/runs/{run_id}/folder", json={"folder_id": "nope"}).status_code == 404


def test_pin_and_unpin(client):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/pin", json={"pinned": True})
    assert row(client, run_id)["pinned"] is True
    client.post(f"/api/runs/{run_id}/pin", json={"pinned": False})
    assert row(client, run_id)["pinned"] is False


def test_first_lock_needs_a_long_enough_code(client):
    run_id = finished_draft(client)
    assert client.post(f"/api/runs/{run_id}/lock", json={"code": "1234567"}).status_code == 400
    assert row(client, run_id)["locked"] is False


def test_locked_draft_hides_everything_until_unlocked(client):
    run_id = finished_draft(client)
    assert client.get("/api/me").json()["has_code"] is False
    assert client.post(f"/api/runs/{run_id}/lock", json={"code": CODE}).status_code == 200
    about = client.get("/api/me").json()
    assert about["has_code"] is True and about["has_locked"] is True

    hidden = row(client, run_id)
    assert hidden["locked"] and hidden["prospect"] is None and hidden["subject"] is None and hidden["confidence"] is None
    assert "Asha" not in client.get("/api/runs").text

    assert client.get(f"/api/runs/{run_id}").status_code == 423
    assert client.get(f"/api/runs/{run_id}/events").status_code == 423
    assert client.post(f"/api/runs/{run_id}/review", json={"action": "discard"}).status_code == 423
    assert client.post(f"/api/runs/{run_id}/redo", json={}).status_code == 423
    assert client.post(f"/api/runs/{run_id}/rename", json={"title": "x"}).status_code == 423

    assert client.post(f"/api/runs/{run_id}/unlock", json={"code": "wrong-code"}).status_code == 403
    assert client.get(f"/api/runs/{run_id}").status_code == 423
    assert client.post(f"/api/runs/{run_id}/unlock", json={"code": CODE}).status_code == 200
    back = client.get(f"/api/runs/{run_id}").json()
    assert back["data"]["draft"]["body"] and back["prospect"]["name"] == "Asha Rao"
    assert any(step["state"] == "done" for step in back["timeline"])


def raw_database_bytes(tmp_path):
    return b"".join(f.read_bytes() for f in tmp_path.iterdir() if f.name.startswith("t.db"))


def test_locking_really_encrypts_what_is_on_disk(client, tmp_path):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/rename", json={"title": "Secret title"})
    before = raw_database_bytes(tmp_path)
    assert b"Asha Rao" in before and b"heroics" in before and b"Secret title" in before

    assert client.post(f"/api/runs/{run_id}/lock", json={"code": CODE}).status_code == 200
    after = raw_database_bytes(tmp_path)
    for secret in (b"Asha Rao", b"Northwind", b"heroics", b"Secret title", b"asha@example", CODE.encode()):
        assert secret not in after, secret
    stored = db.get_run(run_id)
    assert stored["prospect"] == {} and stored["data"] == {} and stored["sealed"] and db.events_since(run_id) == []

    assert client.post(f"/api/runs/{run_id}/unlock", json={"code": CODE}).status_code == 200
    assert client.get(f"/api/runs/{run_id}").json()["title"] == "Secret title"
    assert db.get_run(run_id)["sealed"] is None


def test_encrypted_text_cannot_be_moved_to_another_draft(client):
    first, second = finished_draft(client), finished_draft(client)
    client.post(f"/api/runs/{first}/lock", json={"code": CODE})
    client.post(f"/api/runs/{second}/lock", json={"code": CODE})
    user = db.find_user("Rep One")
    key = lock.open_locker(user["locker"], CODE)
    with pytest.raises(InvalidTag):
        lock.unseal(key, second, db.get_run(first)["sealed"])


def test_work_in_progress_cannot_be_locked(client):
    run_id = finished_draft(client)
    db.update_run(run_id, status="working")
    assert client.post(f"/api/runs/{run_id}/lock", json={"code": CODE}).status_code == 409
    assert db.get_run(run_id)["locked"] is False


def test_one_code_opens_every_private_draft(client):
    first, second = finished_draft(client), finished_draft(client)
    client.post(f"/api/runs/{first}/lock", json={"code": CODE})
    assert client.post(f"/api/runs/{second}/lock", json={"code": ""}).status_code == 403
    assert client.post(f"/api/runs/{second}/lock", json={"code": CODE}).status_code == 200
    assert client.post(f"/api/runs/{second}/unlock", json={"code": CODE}).status_code == 200


def test_too_many_wrong_codes_makes_you_wait(client):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/lock", json={"code": CODE})
    for _ in range(accounts.MAX_WRONG):
        assert client.post(f"/api/runs/{run_id}/unlock", json={"code": "wrong-code"}).status_code == 403
    assert client.post(f"/api/runs/{run_id}/unlock", json={"code": CODE}).status_code == 429


def test_code_is_not_stored_as_typed(client):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/lock", json={"code": "my-secret-code"})
    assert "my-secret-code" not in db.find_user("Rep One")["locker"]


def test_changing_the_code_keeps_locked_drafts_readable(client):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/lock", json={"code": CODE})
    assert client.post("/api/settings/code", json={"current": "wrong-code", "new": "new-code-5555"}).status_code == 403
    assert client.post("/api/settings/code", json={"current": CODE, "new": "new-code-5555"}).status_code == 200
    assert client.post(f"/api/runs/{run_id}/unlock", json={"code": CODE}).status_code == 403
    assert client.post(f"/api/runs/{run_id}/unlock", json={"code": "new-code-5555"}).status_code == 200


def test_forgotten_code_can_be_replaced_only_when_nothing_is_locked(client):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/lock", json={"code": CODE})
    assert client.post("/api/settings/code", json={"new": "fresh-code-1111"}).status_code == 403
    client.post(f"/api/runs/{run_id}/unlock", json={"code": CODE})
    assert client.post("/api/settings/code", json={"new": "fresh-code-1111"}).status_code == 200
    assert client.post(f"/api/runs/{run_id}/lock", json={"code": "fresh-code-1111"}).status_code == 200


def test_a_locked_draft_can_still_be_deleted(client):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/lock", json={"code": CODE})
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    assert db.get_run(run_id) is None


def test_rename_shows_in_the_list_and_on_the_draft(client):
    run_id = finished_draft(client)
    assert client.post(f"/api/runs/{run_id}/rename", json={"title": "  Follow up  "}).status_code == 200
    assert row(client, run_id)["title"] == "Follow up"
    assert client.get(f"/api/runs/{run_id}").json()["title"] == "Follow up"
    assert client.post(f"/api/runs/{run_id}/rename", json={"title": "  "}).status_code == 422


def second_person(tmp_client):
    other = TestClient(main.app)
    assert other.post("/api/signup", json={"name": "Rep Two", "password": "another long one"}).status_code == 200
    return other


def test_you_must_log_in(client):
    stranger = TestClient(main.app)
    assert stranger.get("/api/runs").status_code == 401
    assert stranger.get("/api/me").status_code == 401
    assert stranger.post("/api/runs", json=PERSON).status_code == 401
    assert stranger.get("/api/folders").status_code == 401


def test_people_only_see_their_own_work(client):
    run_id = finished_draft(client)
    folder = client.post("/api/folders", json={"name": "Mine"}).json()["id"]
    other = second_person(client)
    assert run_id not in [r["id"] for r in other.get("/api/runs").json()]
    assert "Mine" not in [f["name"] for f in other.get("/api/folders").json()]
    assert other.get(f"/api/runs/{run_id}").status_code == 404
    assert other.get(f"/api/runs/{run_id}/events").status_code == 404
    assert other.delete(f"/api/runs/{run_id}").status_code == 404
    assert other.post(f"/api/runs/{run_id}/redo", json={}).status_code == 404
    assert other.post(f"/api/runs/{run_id}/lock", json={"code": CODE}).status_code == 404
    assert other.post(f"/api/runs/{run_id}/rename", json={"title": "x"}).status_code == 404
    assert other.delete(f"/api/folders/{folder}").status_code == 404
    mine = other.get("/api/runs").json()[0]["id"]
    assert other.post(f"/api/runs/{mine}/folder", json={"folder_id": folder}).status_code == 404


def test_each_person_has_their_own_code(client):
    run_id = finished_draft(client)
    client.post(f"/api/runs/{run_id}/lock", json={"code": CODE})
    other = second_person(client)
    assert other.get("/api/me").json()["has_code"] is False


def test_log_out_and_back_in(client):
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/login", json={"name": "rep one", "password": "wrong password"}).status_code == 401
    assert client.post("/api/login", json={"name": "rep one", "password": PASSWORD}).status_code == 200
    assert client.get("/api/me").json()["name"] == "Rep One"


def test_changing_user_ends_the_old_session(client):
    old_token = client.cookies.get(main.SESSION_COOKIE)
    second_person(client)
    assert client.post("/api/login", json={"name": "Rep Two", "password": "another long one"}).status_code == 200
    assert client.get("/api/me").json()["name"] == "Rep Two"
    assert db.session_user(accounts.hash_token(old_token)) is None


def test_session_cookie_cannot_be_read_by_page_scripts(client):
    answer = TestClient(main.app).post("/api/signup", json={"name": "Rep Three", "password": "a long password"})
    assert "httponly" in answer.headers["set-cookie"].lower() and "samesite=lax" in answer.headers["set-cookie"].lower()


def test_signup_rules(client):
    fresh = TestClient(main.app)
    assert fresh.post("/api/signup", json={"name": "Rep One", "password": "a long password"}).status_code == 409
    assert fresh.post("/api/signup", json={"name": "REP ONE", "password": "a long password"}).status_code == 409
    assert fresh.post("/api/signup", json={"name": "New Rep", "password": "short"}).status_code == 400
    assert fresh.post("/api/signup", json={"name": "x", "password": "a long password"}).status_code == 400


def test_too_many_wrong_passwords_makes_you_wait(client):
    fresh = TestClient(main.app)
    for _ in range(accounts.MAX_WRONG):
        assert fresh.post("/api/login", json={"name": "Rep One", "password": "wrong password"}).status_code == 401
    assert fresh.post("/api/login", json={"name": "Rep One", "password": PASSWORD}).status_code == 429


def test_password_is_not_stored_as_typed(client):
    assert PASSWORD not in db.find_user("Rep One")["password"]


def test_changing_password_logs_out_other_devices(client):
    other_device = TestClient(main.app)
    other_device.post("/api/login", json={"name": "Rep One", "password": PASSWORD})
    assert client.post("/api/settings/password", json={"current": "nope nope", "new": "a new long password"}).status_code == 403
    assert client.post("/api/settings/password", json={"current": PASSWORD, "new": "a new long password"}).status_code == 200
    assert other_device.get("/api/me").status_code == 401
    assert client.get("/api/me").status_code == 200


def test_settings_name_signs_new_emails(client):
    assert client.post("/api/settings/profile", json={"name": "Rep Renamed", "sender": "Rep R. from Sales"}).status_code == 200
    about = client.get("/api/me").json()
    assert about["name"] == "Rep Renamed" and about["sender"] == "Rep R. from Sales"
    run_id = finished_draft(client)
    assert "Best,\nRep R. from Sales\nZamp" in client.get(f"/api/runs/{run_id}").json()["data"]["draft"]["body"]


def test_settings_name_must_be_free(client):
    second_person(client)
    assert client.post("/api/settings/profile", json={"name": "Rep Two"}).status_code == 409


def test_delete_removes_the_draft_and_its_history(client):
    run_id = finished_draft(client)
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert db.events_since(run_id) == []


def test_work_in_progress_cannot_be_deleted_or_redone(client):
    run_id = finished_draft(client)
    db.update_run(run_id, status="working")
    assert client.delete(f"/api/runs/{run_id}").status_code == 409
    assert client.post(f"/api/runs/{run_id}/redo", json={}).status_code == 409
    assert db.get_run(run_id) is not None


def test_redo_starts_fresh_keeps_the_old_one_and_stays_in_its_folder(client):
    run_id = client.post("/api/runs", json={**PERSON, "email": "asha@northwind.test"}).json()["id"]
    client.post(f"/api/runs/{run_id}/confirm", json={"person": 0})
    folder = client.post("/api/folders", json={"name": "Finance"}).json()["id"]
    client.post(f"/api/runs/{run_id}/folder", json={"folder_id": folder})

    new_id = client.post(f"/api/runs/{run_id}/redo", json={}).json()["id"]
    assert new_id != run_id
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "ready"
    fresh = client.get(f"/api/runs/{new_id}").json()
    assert fresh["status"] == "waiting" and fresh["folder_id"] == folder
    assert fresh["prospect"]["email"] == "asha@northwind.test"


def test_older_databases_get_the_new_columns(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, status TEXT NOT NULL, prospect TEXT NOT NULL, data TEXT NOT NULL)")
    conn.execute("INSERT INTO runs VALUES ('abc', 1, 1, 'ready', '{}', '{}')")
    conn.commit()
    conn.close()
    db.init(path)
    run = db.get_run("abc")
    assert run["pinned"] is False and run["locked"] is False and run["folder_id"] is None


def test_new_accounts_get_test_drafts_that_open_like_real_ones(client):
    rows = client.get("/api/runs").json()
    assert len(rows) == len(samples.SAMPLES)
    assert {f["name"] for f in client.get("/api/folders").json()} == set(samples.FOLDERS)
    assert any(r["pinned"] for r in rows)
    for item in rows:
        data = client.get(f"/api/runs/{item['id']}").json()["data"]
        assert data["sample"] is True and data["draft"]["body"].startswith("Hi ")
        assert "Best,\nRep One\nZamp" in data["draft"]["body"]
        assert 0 <= data["confidence"]["score"] <= 100


def test_deleted_test_drafts_do_not_come_back(client):
    first = client.get("/api/runs").json()[0]["id"]
    client.delete(f"/api/runs/{first}")
    client.post("/api/logout", json={})
    client.post("/api/login", json={"name": "Rep One", "password": PASSWORD})
    assert len(client.get("/api/runs").json()) == len(samples.SAMPLES) - 1


def test_first_account_takes_over_older_drafts_instead_of_samples(tmp_path):
    db.init(str(tmp_path / "t.db"))
    old = db.create_run(PERSON)
    db.update_run(old, status="ready", draft={"subject": "s", "body": "Hi,\n\nBest,\n[Your name]\nZamp"})
    first = TestClient(main.app)
    first.post("/api/signup", json={"name": "First Rep", "password": PASSWORD})
    assert [r["id"] for r in first.get("/api/runs").json()] == [old]
    assert first.get(f"/api/runs/{old}").json()["data"]["draft"]["body"].endswith("Best,\nFirst Rep\nZamp")
    second = TestClient(main.app)
    second.post("/api/signup", json={"name": "Second Rep", "password": PASSWORD})
    assert old not in [r["id"] for r in second.get("/api/runs").json()]
    assert len(second.get("/api/runs").json()) == len(samples.SAMPLES)


def test_a_sample_can_be_finished(client):
    item = next(r for r in client.get("/api/runs").json() if r["status"] == "ready")
    answer = client.post(f"/api/runs/{item['id']}/review", json={"action": "approve", "subject": "s", "body": "b", "card_headline": "c"})
    assert answer.status_code == 200
