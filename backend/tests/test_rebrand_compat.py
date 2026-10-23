"""Migration regressions use temporary files and synthetic credentials only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from aiopsstudio.compat import (environment, state_directory, TABLE_PREFIX, LEGACY_TABLE_PREFIX,
    TABLE_SUFFIXES, COOKIE_NAME, LEGACY_COOKIE_NAME, docker_state_matches)
from aiopsstudio.main import create_app
from aiopsstudio.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_legacy_environment_migrates_once_and_rejects_conflicts_without_values():
    legacy = {"EASYOPS_DATABASE_URL": "synthetic-private-address", "EASYOPS_MASTER_KEY_FILE": "/synthetic/key", "OTHER": "retained"}
    current = environment(legacy)
    assert current == {"AIOPSSTUDIO_DATABASE_URL": "synthetic-private-address", "AIOPSSTUDIO_MASTER_KEY_FILE": "/synthetic/key", "OTHER": "retained"}
    assert environment({**legacy, "AIOPSSTUDIO_DATABASE_URL": legacy["EASYOPS_DATABASE_URL"]}) == current
    with pytest.raises(ValueError) as caught:
        environment({**legacy, "AIOPSSTUDIO_DATABASE_URL": "synthetic-other-address"})
    assert "synthetic-private-address" not in str(caught.value)
    assert "synthetic-other-address" not in str(caught.value)


@pytest.mark.parametrize("legacy_name", ["easyops", "easyops-workbench"])
def test_state_discovery_reuses_old_data_without_move_and_rejects_split(tmp_path, legacy_name):
    old = tmp_path / ".local/share" / legacy_name
    assert state_directory({}, home=tmp_path) == tmp_path / ".local/share/aiopsstudio"
    old.mkdir(parents=True)
    marker = old / "synthetic-history"
    marker.write_text("must remain in its original location")
    assert state_directory({}, home=tmp_path) == old
    current = tmp_path / ".local/share/aiopsstudio"
    current.mkdir()
    with pytest.raises(ValueError, match="多个状态目录"):
        state_directory({}, home=tmp_path)
    assert state_directory({"AIOPSSTUDIO_STATE_DIR": str(old)}, home=tmp_path) == old
    assert marker.read_text() == "must remain in its original location"
    assert not any(current.iterdir())


def test_deployment_keeps_legacy_database_assets_and_explicit_overrides(tmp_path):
    # Import deployment helpers only: no runtime initialization or Docker access.
    from deploy.settings import load_config
    old = tmp_path / "previous-private-state"
    config = load_config(environ={"EASYOPS_STATE_DIR": str(old)})
    assert config["state_dir"] == old
    assert config["database"]["container"] == "easyops-db"
    assert config["database"]["volume"] == "easyops-db-data"
    settings = tmp_path / "deployment.json"
    settings.write_text(json.dumps({"database": {"container": "existing-db", "volume": "existing-volume", "user": "existing-user", "name": "existing-name"}}))
    overridden = load_config(settings, environ={"AIOPSSTUDIO_STATE_DIR": str(old)})
    assert overridden["database"]["container"] == "existing-db"
    assert overridden["database"]["volume"] == "existing-volume"
    assert not old.exists()


def test_same_new_state_key_does_not_change_legacy_database_identity(tmp_path):
    from deploy.settings import load_config
    old = tmp_path / 'custom-existing-state'
    before = load_config(environ={'EASYOPS_STATE_DIR': str(old)})
    after = load_config(environ={'EASYOPS_STATE_DIR': str(old), 'AIOPSSTUDIO_STATE_DIR': str(old)})
    assert after['database'] == before['database']
    assert after['database']['container'] == 'easyops-db'
    assert not old.exists()


@pytest.mark.parametrize('prefix', ['AIOPSSTUDIO_', 'EASYOPS_'])
@pytest.mark.parametrize('invalid', ['', 'relative-state'])
def test_explicit_invalid_state_does_not_silently_select_default(prefix, invalid):
    from deploy.settings import load_config
    with pytest.raises(ValueError, match='必须是绝对路径'):
        load_config(environ={prefix + 'STATE_DIR': invalid})


@pytest.mark.parametrize('name', ['custom-state', 'easyops', 'aiopsstudio'])
@pytest.mark.parametrize('already_exists', [False, True])
def test_new_custom_state_requires_database_identity_before_any_initialization(tmp_path, name, already_exists):
    from deploy.settings import load_config
    state = tmp_path / name
    if already_exists:
        state.mkdir()
        (state / 'synthetic-history').write_text('must remain unchanged')
    with pytest.raises(ValueError, match='自定义状态目录必须显式配置'):
        load_config(environ={'AIOPSSTUDIO_STATE_DIR': str(state)})
    assert state.exists() is already_exists
    if already_exists:
        assert list(state.iterdir()) == [state / 'synthetic-history']
        assert (state / 'synthetic-history').read_text() == 'must remain unchanged'


@pytest.mark.parametrize('missing', ['container', 'volume', 'user', 'name'])
def test_partial_database_identity_does_not_guess_custom_state_defaults(tmp_path, missing):
    from deploy.settings import DEFAULT_DATABASE, load_config
    database = {key: value for key, value in DEFAULT_DATABASE.items() if key != missing}
    settings = tmp_path / 'custom-deployment.json'
    settings.write_text(json.dumps({'database': database}))
    with pytest.raises(ValueError, match='自定义状态目录必须显式配置'):
        load_config(settings, environ={'AIOPSSTUDIO_STATE_DIR': str(tmp_path / 'custom-state')})
    assert not (tmp_path / 'custom-state').exists()


def test_explicit_custom_database_identity_is_stable_before_and_after_first_start(tmp_path):
    from deploy.settings import DEFAULT_DATABASE, load_config
    state = tmp_path / 'custom-state'
    settings = tmp_path / 'custom-deployment.json'
    settings.write_text(json.dumps({'database': DEFAULT_DATABASE}))
    environ = {'AIOPSSTUDIO_STATE_DIR': str(state)}
    before = load_config(settings, environ=environ)
    assert not state.exists()
    state.mkdir()
    (state / 'synthetic-history').write_text('saved after initial setup')
    after = load_config(settings, environ=environ)
    assert after == before
    assert after['database'] == DEFAULT_DATABASE
    assert (state / 'synthetic-history').read_text() == 'saved after initial setup'


def test_external_database_url_allows_custom_state_without_guessing_managed_database(tmp_path):
    from deploy.settings import load_config
    state = tmp_path / 'custom-state'
    config = load_config(environ={'AIOPSSTUDIO_STATE_DIR': str(state),
        'AIOPSSTUDIO_DATABASE_URL': 'postgresql://synthetic.example.invalid/database'})
    assert config['state_dir'] == state
    assert not state.exists()


@pytest.mark.parametrize('name', ['easyops', 'easyops-workbench'])
def test_recognized_old_default_directory_retains_database_defaults_with_new_key(tmp_path, monkeypatch, name):
    from deploy.settings import load_config
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    state = tmp_path / '.local/share' / name
    state.mkdir(parents=True)
    detected = load_config(environ={})
    explicit = load_config(environ={'AIOPSSTUDIO_STATE_DIR': str(state)})
    assert detected == explicit
    assert explicit['database']['container'] == 'easyops-db'
    assert not list(state.iterdir())


def table_names(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_store_uses_current_namespace_on_empty_database(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "current.sqlite"))
    try:
        assert store.namespace == TABLE_PREFIX
        assert table_names(store.conn) == {f"{TABLE_PREFIX}_{suffix}" for suffix in TABLE_SUFFIXES}
    finally:
        store.close()


def test_old_database_retains_documents_events_lease_and_revisions(tmp_path):
    path = tmp_path / "previous.sqlite"
    url = "sqlite:///" + str(path)
    seed = Store(url)
    try:
        doc = seed.save("conversation", "owned-history", {"owner_id": "synthetic-user", "messages": [{"content": "saved history"}]})
        seed.save("secret", "synthetic-secret", {"ciphertext": "opaque-unchanged"})
        seed.save("run", "active-run", {"owner_id": "synthetic-user", "status": "running"})
        first = seed.event("active-run", "saved-event", {"value": "preserve"})
        seed.conn.execute(f"UPDATE {TABLE_PREFIX}_worker_slot SET owner=?,run_id=?,expires_at=? WHERE id=1", ("existing-worker", "active-run", time.time() + 120))
        for suffix in TABLE_SUFFIXES:
            seed.conn.execute(f"ALTER TABLE {TABLE_PREFIX}_{suffix} RENAME TO {LEGACY_TABLE_PREFIX}_{suffix}")
    finally:
        seed.close()
    reopened = Store(url)
    try:
        assert reopened.namespace == LEGACY_TABLE_PREFIX
        assert table_names(reopened.conn) == {f"{LEGACY_TABLE_PREFIX}_{suffix}" for suffix in TABLE_SUFFIXES}
        assert reopened.get("conversation", "owned-history") == doc
        assert reopened.get("secret", "synthetic-secret")["ciphertext"] == "opaque-unchanged"
        assert reopened.events("active-run") == [first]
        assert reopened.claim_job("new-worker") is None
        assert reopened.renew_job("active-run", "existing-worker") is True
        assert reopened.release_job("active-run", "existing-worker") is True
        assert reopened.event("active-run", "new-event", {})["seq"] == first["seq"] + 1
        assert reopened.save("conversation", "owned-history", doc, expected_revision=doc["revision"])["revision"] == doc["revision"] + 1
    finally:
        reopened.close()


@pytest.mark.parametrize("tables", [("easyops_documents",), ("aiopsstudio_documents",), ("easyops_documents", "aiopsstudio_documents")])
def test_ambiguous_or_partial_table_sets_are_rejected_without_new_tables(tmp_path, tables):
    path = tmp_path / "partial.sqlite"
    with sqlite3.connect(path) as conn:
        for table in tables:
            conn.execute(f"CREATE TABLE {table} (synthetic TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES ('must-preserve')")
    with pytest.raises(ValueError, match="冲突或不完整"):
        Store("sqlite:///" + str(path))
    with sqlite3.connect(path) as conn:
        assert table_names(conn) == set(tables)
        assert all(conn.execute(f"SELECT synthetic FROM {table}").fetchall() == [("must-preserve",)] for table in tables)


def test_docker_ownership_accepts_old_or_new_labels_and_rejects_ambiguity():
    assert docker_state_matches("owner-id|<no value>", "owner-id")
    assert docker_state_matches("<no value>|owner-id", "owner-id")
    assert docker_state_matches("owner-id|owner-id", "owner-id")
    assert not docker_state_matches("owner-id|different-id", "owner-id")
    assert not docker_state_matches("<no value>|<no value>", "owner-id")


@pytest.fixture
def session_app(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "sessions.sqlite"))
    app = create_app(store=store, testing=True, enable_runtime=False, master_key=Fernet.generate_key(),
        password="synthetic-login-password", artifact_dir=str(tmp_path / "artifacts"))
    client = TestClient(app, base_url="http://testserver", headers={"Origin": "http://127.0.0.1:3000"})
    yield app, client, store
    client.close()
    store.close()


def seed_session(store, token, **overrides):
    sid = hashlib.sha256(token.encode()).hexdigest()
    store.save("session", sid, {"user_id": "admin", "expires_at": time.time() + 300, **overrides})
    return sid


def test_valid_old_cookie_migrates_with_existing_expiry_and_logout_revokes(session_app):
    app, client, store = session_app
    sid = seed_session(store, "synthetic-legacy-token")
    before = store.get("session", sid)
    response = client.get("/api/session", headers={"Cookie": f"{LEGACY_COOKIE_NAME}=synthetic-legacy-token"})
    assert response.json()["authenticated"] is True
    assert response.cookies.get(COOKIE_NAME) == "synthetic-legacy-token"
    headers = response.headers.get_list("set-cookie")
    assert any(LEGACY_COOKIE_NAME + '=""' in value and "Max-Age=0" in value for value in headers)
    assert any(COOKIE_NAME + "=" in value and "HttpOnly" in value and "SameSite=strict" in value for value in headers)
    assert store.get("session", sid) == before
    assert client.post("/api/logout", json={}).status_code == 200
    assert store.get("session", sid) is None
    assert client.get("/api/session", headers={"Cookie": f"{LEGACY_COOKIE_NAME}=synthetic-legacy-token"}).json()["authenticated"] is False


def test_cookie_conflict_rejects_identity_selection_and_clears_both(session_app):
    _, client, store = session_app
    seed_session(store, "synthetic-new-token")
    seed_session(store, "synthetic-old-token")
    response = client.get("/api/session", headers={"Cookie": f"{COOKIE_NAME}=synthetic-new-token; {LEGACY_COOKIE_NAME}=synthetic-old-token"})
    assert response.status_code == 401
    assert response.json()["code"] == "session_cookie_conflict"
    assert len(response.headers.get_list("set-cookie")) == 2


def test_old_cookie_does_not_extend_expired_or_overlong_session(session_app):
    _, client, store = session_app
    seed_session(store, "synthetic-expired-token", expires_at=time.time() - 1)
    assert client.get("/api/session", headers={"Cookie": f"{LEGACY_COOKIE_NAME}=synthetic-expired-token"}).json()["authenticated"] is False
    seed_session(store, "synthetic-overlong-token", created_at=(datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(), expires_at=time.time() + 3600)
    assert client.get("/api/session", headers={"Cookie": f"{LEGACY_COOKIE_NAME}=synthetic-overlong-token"}).json()["authenticated"] is False


def test_new_login_issues_only_current_cookie_and_discards_old_cookie(session_app):
    app, client, store = session_app
    assert app.title == "AIOpsStudio 工作台"
    response = client.post("/api/login", json={"password": "synthetic-login-password"})
    assert response.status_code == 200
    assert response.cookies.get(COOKIE_NAME)
    assert LEGACY_COOKIE_NAME not in response.cookies


def test_factory_uses_legacy_environment_without_rewriting_private_files(tmp_path, monkeypatch):
    files = {"master": Fernet.generate_key().decode(), "password": "synthetic-legacy-password"}
    for name, content in files.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o600)
    legacy = {"DATABASE_URL": "sqlite:///" + str(tmp_path / "factory.sqlite"), "MASTER_KEY_FILE": str(tmp_path / "master"),
        "PASSWORD_FILE": str(tmp_path / "password"), "ARTIFACT_DIR": str(tmp_path / "artifacts"), "PUBLIC_ORIGIN": "https://workbench.example.invalid"}
    for suffix, value in legacy.items():
        monkeypatch.delenv("AIOPSSTUDIO_" + suffix, raising=False)
        monkeypatch.setenv("EASYOPS_" + suffix, value)
    app = create_app(testing=True, enable_runtime=False)
    try:
        assert app.state.store.database_url == legacy["DATABASE_URL"]
        assert app.state.public_origin == legacy["PUBLIC_ORIGIN"]
        assert app.state.identity.authenticate("admin", files["password"])["id"] == "admin"
        assert all((tmp_path / name).read_text() == content for name, content in files.items())
    finally:
        app.state.store.close()


def test_deployment_child_environment_is_canonical_and_preserves_explicit_material(tmp_path, monkeypatch):
    import importlib.util
    for key in list(os.environ):
        if key.startswith(("EASYOPS_", "AIOPSSTUDIO_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("EASYOPS_STATE_DIR", str(tmp_path / "old-state"))
    monkeypatch.setenv("EASYOPS_DATABASE_URL", "postgresql://synthetic.example.invalid/database")
    monkeypatch.setenv("EASYOPS_MASTER_KEY_FILE", str(tmp_path / "external-master"))
    spec = importlib.util.spec_from_file_location("isolated_rebrand_deployment", Path(__file__).resolve().parents[2] / "deploy/local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    child_env = module.env()
    assert child_env["AIOPSSTUDIO_DATABASE_URL"] == "postgresql://synthetic.example.invalid/database"
    assert child_env["AIOPSSTUDIO_MASTER_KEY_FILE"] == str(tmp_path / "external-master")
    assert child_env["AIOPSSTUDIO_PRIVATE_DIR"] == str(tmp_path / "old-state/private")
    assert not any(key.startswith("EASYOPS_") for key in child_env)
    assert not (tmp_path / "external-master").exists()
