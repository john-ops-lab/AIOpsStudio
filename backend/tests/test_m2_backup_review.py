"""Backup quiescence and independent-key oracles; only temporary local data."""
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from aiopsstudio.backup import BackupError, create_backup
from aiopsstudio import backup
from aiopsstudio.config import Vault
from aiopsstudio.store import Store


class _Workspace(tuple):
    def __repr__(self):
        return "<synthetic backup workspace; key omitted>"


@pytest.fixture
def workspace(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "source.db"))
    root = tmp_path / "assets"
    root.mkdir()
    key = Fernet.generate_key()
    Vault(store, key).put("synthetic-vault-value")
    store.save("settings", "main", {"artifact_dir": str(root)})
    (root / "evidence.json").write_bytes(b'{"synthetic":true}')
    yield _Workspace((store, root, key))
    store.close()


@pytest.mark.parametrize("expired", [False, True])
def test_terminal_run_does_not_make_unreleased_delivery_lease_safe_to_backup(workspace, tmp_path, expired):
    store, root, key = workspace
    store.save("run", "delivery", {"status": "queued", "usage": {"model_http_request_count": 1}})
    claimed = store.claim_job("delivery-worker")
    record = store.get("run", claimed["id"])
    record.update(status="partial", report={"analysis": "saved; artifact delivery not finished"})
    store.save("run", record["id"], record)
    if expired:
        store._execute(f"UPDATE {store._worker_slot} SET expires_at=0 WHERE id=1")
    before = store.get("run", record["id"])
    slot = tuple(store._execute(f"SELECT * FROM {store._worker_slot}").fetchone())
    with pytest.raises(BackupError):
        create_backup(store, root, tmp_path / "rejected", master_key=key)
    assert not (tmp_path / "rejected").exists()
    assert store.get("run", record["id"]) == before
    assert tuple(store._execute(f"SELECT * FROM {store._worker_slot}").fetchone()) == slot
    assert len(store.list("run")) == 1
    # The execution owner explicitly completes its lease, not the backup path.
    assert store.release_job(record["id"], "delivery-worker")
    assert create_backup(store, root, tmp_path / "finished", master_key=key)["files"]


@pytest.mark.parametrize("placement", ["exact", "surrounded", "cross-chunk"])
def test_api_without_key_path_refuses_same_plaintext_key_in_artifacts(workspace, tmp_path, placement):
    store, root, key = workspace
    if placement == "exact":
        payload = key + b"\n"
    elif placement == "surrounded":
        payload = b'synthetic_configuration="' + key + b'"\n'
    else:
        payload = b"x" * (1024 * 1024 - len(key) // 2) + key + b"end"
    key_file = root / "private-material.txt"
    key_file.write_bytes(payload)
    before = hashlib.sha256(payload).hexdigest()
    with pytest.raises(BackupError) as error:
        create_backup(store, root, tmp_path / "rejected", master_key=key)
    assert not (tmp_path / "rejected").exists()
    assert hashlib.sha256(key_file.read_bytes()).hexdigest() == before
    assert key.decode() not in str(error.value)


@pytest.mark.parametrize("alias", [False, True])
def test_explicit_key_location_under_artifact_root_rejects_before_creating_package(workspace, tmp_path, monkeypatch, alias):
    store, root, key = workspace
    source = root / "master.key"
    source.write_bytes(key + b"\n")
    if alias:
        location = tmp_path / "external-looking-key"
        location.symlink_to(source)
    else:
        location = source

    def should_not_enumerate(*args):
        raise AssertionError("known key location must reject before file enumeration")

    monkeypatch.setattr(backup, "_safe_files", should_not_enumerate)
    with pytest.raises(BackupError) as error:
        create_backup(store, root, tmp_path / "rejected", master_key=key, master_key_path=location)
    assert error.value.code == "master_key_in_artifacts"
    assert key.decode() not in str(error.value)
    assert not (tmp_path / "rejected").exists()
    assert source.read_bytes() == key + b"\n"


def test_external_independent_key_is_not_in_backup(workspace, tmp_path):
    store, root, key = workspace
    key_file = tmp_path / "external-master.key"
    key_file.write_bytes(key + b"\n")
    output = tmp_path / "backup"
    manifest = create_backup(store, root, output, master_key=key, master_key_path=key_file)
    assert manifest["files"]
    assert all(key not in path.read_bytes() for path in output.rglob("*") if path.is_file())


def test_database_plaintext_key_is_rejected_before_writing_backup_file(workspace, tmp_path, monkeypatch):
    store, root, key = workspace
    message = store.save("conversation", "synthetic-message", {
        "messages": [{"role": "user", "content": "synthetic pasted value: " + key.decode()}]})
    output, writes = tmp_path / "rejected", []
    original = Path.write_bytes

    def observe(path, payload):
        if path == output / "database.json":
            writes.append(True)
        return original(path, payload)

    monkeypatch.setattr(Path, "write_bytes", observe)
    with pytest.raises(BackupError) as error:
        create_backup(store, root, output, master_key=key)
    assert error.value.code == "master_key_in_database"
    assert key.decode() not in str(error.value)
    assert writes == []
    assert not output.exists()
    assert store.get("conversation", "synthetic-message") == message


def test_database_normal_business_text_and_other_values_are_preserved(workspace, tmp_path):
    store, root, key = workspace
    # Same-shaped but different text is ordinary backup content, not this key.
    other = (b"A" if key[:1] != b"A" else b"B") + key[1:]
    message = store.save("conversation", "synthetic-message", {
        "messages": [{"role": "user", "content": other.decode()}], "note": key[:20].decode()})
    output = tmp_path / "backup"
    assert create_backup(store, root, output, master_key=key)["files"]
    payload = (output / "database.json").read_bytes()
    assert other in payload and key not in payload
    assert store.get("conversation", "synthetic-message") == message
