"""Offline, bounded application backups; restore only into an empty target.

A backup contains private data. It is never an HTTP/public export and never
contains the independent master key. Restored state is quarantined until an
operator supplies current authorization/revocation facts from outside backup.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from .store import utcnow

CHECKPOINT_TABLES = {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
MAX_BACKUP_BYTES = 256 * 1024 * 1024


class BackupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__("备份或恢复未完成（" + code + "）")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _key(value):
    value = value.encode() if isinstance(value, str) else value
    if not isinstance(value, bytes) or len(value) < 32:
        raise BackupError("key_missing")
    return value


def _cell(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "bytes", "value": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, (datetime, date, Decimal, UUID)):
        return {"type": type(value).__name__, "value": str(value)}
    return {"type": "json", "value": value}


def _decode(cell):
    kind, value = cell["type"], cell["value"]
    if kind == "bytes":
        return base64.b64decode(value, validate=True)
    if kind == "datetime":
        return datetime.fromisoformat(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "Decimal":
        return Decimal(value)
    if kind == "UUID":
        return UUID(value)
    if kind != "json":
        raise BackupError("cell_type")
    return value


def _tables(store):
    result = {store._documents, store._events, store._worker_slot}
    if not store.sqlite:
        present = {r[0] for r in store._execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'")}
        checkpoint = present & CHECKPOINT_TABLES
        if checkpoint and checkpoint != CHECKPOINT_TABLES:
            raise BackupError("checkpoint_tables_incomplete")
        result |= checkpoint
    return sorted(result)


def _safe_files(root):
    root = Path(root).resolve()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError("artifact_symlink")
        if path.is_file():
            yield path, path.relative_to(root).as_posix()


def _check_references(store, root, files):
    # Inspect only declared file-path fields, never arbitrary source text.
    from .attachments import AttachmentsService
    attachment_paths = AttachmentsService.__new__(AttachmentsService)
    attachment_paths.artifact_dir, attachment_paths.root = root, root / "attachments"
    entries = {entry["path"]: entry for entry in files}
    for kind in ("evidence", "attachment", "material_read"):
        for record in store.list(kind):
            if record.get("deleted_at") or record.get("expired"):
                continue
            value = record.get("path")
            if kind == "attachment" and not value:
                try:
                    value = attachment_paths._path(record)
                except Exception:
                    raise BackupError("artifact_reference_path") from None
            if not value:
                continue
            try:
                relative = Path(value).resolve().relative_to(root).as_posix()
            except ValueError:
                raise BackupError("artifact_reference_outside_root") from None
            if relative not in entries:
                raise BackupError("artifact_reference_missing")
            entry = entries[relative]
            expected_size = record.get("size", record.get("bytes"))
            if record.get("sha256") and record["sha256"] != entry["sha256"] or expected_size is not None and expected_size != entry["bytes"]:
                raise BackupError("artifact_reference_integrity")
    for skill in store.list("skill"):
        versions = skill.get("versions", [])
        versions = versions.values() if isinstance(versions, dict) else versions
        for version in versions:
            snapshot = version.get("snapshot") or version.get("data") or version
            package = snapshot.get("artifact_path")
            if not package:  # M1 versions did not declare a published package.
                continue
            try:
                directory = Path(package).resolve().relative_to(root).as_posix()
            except ValueError:
                raise BackupError("skill_reference_outside_root") from None
            declared = {"SKILL.md": snapshot.get("content", ""), **snapshot.get("references", {})}
            expected = set()
            for name, content in declared.items():
                relative = directory + "/" + name
                expected.add(relative)
                entry = entries.get(relative)
                if not entry:
                    raise BackupError("skill_reference_missing")
                encoded = content.encode("utf-8")
                if entry["sha256"] != sha(encoded) or entry["bytes"] != len(encoded):
                    raise BackupError("skill_reference_integrity")
            if {name for name in entries if name.startswith(directory + "/")} != expected:
                raise BackupError("skill_reference_set")


def create_backup(store, artifact_dir, destination, *, master_key, master_key_path=None, max_bytes=MAX_BACKUP_BYTES):
    """Acquire the same writer lock, reject active jobs, copy one consistency set."""
    key = _key(master_key)
    root, target = Path(artifact_dir).resolve(), Path(destination).resolve()
    if master_key_path is not None and Path(master_key_path).resolve().is_relative_to(root):
        raise BackupError("master_key_in_artifacts")
    if target.exists() or target == root or target.is_relative_to(root):
        raise BackupError("destination_not_empty")
    if not root.is_dir() or not 1 <= max_bytes <= 1024 * 1024 * 1024:
        raise BackupError("backup_input")
    target.mkdir(parents=True, mode=0o700)
    try:
        def capture():
            slot = store._execute(f"SELECT owner,run_id,expires_at FROM {store._worker_slot} WHERE id=1").fetchone()
            # Terminal run status precedes conversation/file delivery. An
            # unreleased slot, even expired, is not a completed consistency point.
            # Only the execution/recovery path may clear it; backup stays read-only.
            if (not slot or slot[0] is not None or slot[1] is not None or slot[2] != 0
                    or any(r.get("status") in {"queued", "running"} for r in store.list("run"))):
                raise BackupError("active_execution")
            from cryptography.fernet import Fernet
            try:
                cipher = Fernet(key)
                for secret in store.list("secret"):
                    cipher.decrypt(secret["ciphertext"].encode())
            except Exception:
                raise BackupError("key_mismatch") from None
            tables = []
            encoded_bytes = 0
            for table in _tables(store):
                if not re.fullmatch(r"[a-z_]+", table):
                    raise BackupError("table_name")
                cursor = store._execute(f'SELECT * FROM "{table}"')
                columns = [getattr(c, "name", c[0]) for c in cursor.description]
                rows = []
                for row in cursor:
                    encoded = [_cell(value) for value in row]
                    encoded_bytes += len(canonical(encoded))
                    if encoded_bytes > max_bytes:
                        raise BackupError("backup_limit")
                    rows.append(encoded)
                tables.append({"name": table, "columns": columns, "rows": rows})
            database = canonical({"engine": "sqlite" if store.sqlite else "postgresql", "namespace": store.namespace, "tables": tables})
            if key in database:
                raise BackupError("master_key_in_database")
            total = len(database)
            if total > max_bytes:
                raise BackupError("backup_limit")
            (target / "database.json").write_bytes(database)
            (target / "database.json").chmod(0o600)
            files = []
            assets = target / "artifacts"
            assets.mkdir(mode=0o700)
            for source, relative in _safe_files(root):
                size = source.stat().st_size
                total += size
                if total > max_bytes or shutil.disk_usage(target).free < size + 64 * 1024 * 1024:
                    raise BackupError("backup_limit")
                before = source.stat()
                output = assets / relative
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Bound reads per file; the total set has already been checked.
                h = hashlib.sha256()
                with source.open("rb") as reader, output.open("xb") as writer:
                    tail = b""
                    while chunk := reader.read(1024 * 1024):
                        examined = tail + chunk
                        if key in examined:
                            raise BackupError("master_key_in_artifacts")
                        tail = examined[-(len(key) - 1):]
                        writer.write(chunk); h.update(chunk)
                output.chmod(0o600)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise BackupError("artifact_changed")
                files.append({"path": relative, "bytes": size, "sha256": h.hexdigest(), "mode": before.st_mode & 0o777})
            _check_references(store, root, files)
            manifest = {"schema": "aiopsstudio-private-backup/1", "created_at": utcnow(),
                "database": {"file": "database.json", "bytes": len(database), "sha256": sha(database)},
                "source_artifact_root": str(root), "files": files, "total_bytes": total,
                "tables": [t["name"] for t in tables], "key_fingerprint": sha(key),
                "skill_versions": [{"id": s["id"], "version": s.get("current_version"), "revision": s.get("revision")} for s in store.list("skill")],
                "authorization": "backup_is_not_current_revocation_proof"}
            manifest["signature"] = hmac.new(key, canonical(manifest), hashlib.sha256).hexdigest()
            (target / "manifest.json").write_bytes(canonical(manifest))
            (target / "manifest.json").chmod(0o600)
            return manifest
        return store.atomic(capture)
    except BaseException:
        shutil.rmtree(target)
        raise


def verify_backup(backup_dir, *, master_key):
    key, root = _key(master_key), Path(backup_dir).resolve()
    try:
        if root.is_symlink() or not (root / "manifest.json").is_file():
            raise BackupError("manifest_missing")
        manifest = json.loads((root / "manifest.json").read_bytes())
        signature = manifest.pop("signature")
        if manifest.get("schema") != "aiopsstudio-private-backup/1" or manifest.get("key_fingerprint") != sha(key):
            raise BackupError("key_mismatch")
        if not hmac.compare_digest(signature, hmac.new(key, canonical(manifest), hashlib.sha256).hexdigest()):
            raise BackupError("manifest_integrity")
        manifest["signature"] = signature
        database = (root / "database.json").read_bytes()
        if len(database) != manifest["database"]["bytes"] or sha(database) != manifest["database"]["sha256"]:
            raise BackupError("database_integrity")
        expected = set()
        for entry in manifest["files"]:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise BackupError("artifact_path")
            path = root / "artifacts" / relative
            if path.is_symlink() or not path.is_file():
                raise BackupError("artifact_missing")
            h = hashlib.sha256()
            with path.open("rb") as reader:
                while chunk := reader.read(1024 * 1024): h.update(chunk)
            if path.stat().st_size != entry["bytes"] or h.hexdigest() != entry["sha256"]:
                raise BackupError("artifact_integrity")
            expected.add(relative.as_posix())
        if {p for _, p in _safe_files(root / "artifacts")} != expected:
            raise BackupError("artifact_set")
        return manifest, json.loads(database)
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_incomplete") from None


def _assert_empty_target(store):
    existing = store._execute(f"SELECT kind,id FROM {store._documents}").fetchall()
    if any(kind != "schema_migration" for kind, _ in existing) or store._execute(f"SELECT COUNT(*) FROM {store._events}").fetchone()[0]:
        raise BackupError("target_database_not_empty")
    slot = store._execute(f"SELECT owner,run_id,expires_at,generation FROM {store._worker_slot} WHERE id=1").fetchone()
    if not slot or slot[0] is not None or slot[1] is not None or slot[2] != 0 or slot[3] != 0:
        raise BackupError("target_worker_not_empty")
    if store.sqlite:
        tables = {r[0] for r in store._execute("SELECT name FROM sqlite_master WHERE type='table'")}
    else:
        tables = {r[0] for r in store._execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()")}
    for table in CHECKPOINT_TABLES - {"checkpoint_migrations"}:
        if table in tables and store._execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
            raise BackupError("target_checkpoints_not_empty")


def _relocate_document(kind, document, original_root, target):
    """Move server-owned locators at known schema positions, never business JSON."""
    document = copy.deepcopy(document)

    def locator(record, field):
        if not isinstance(record, dict):
            return
        value = record.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            return
        try:
            relative = Path(value).resolve().relative_to(original_root)
        except ValueError:
            return
        record[field] = str(target / relative)

    def skill(record):
        if not isinstance(record, dict):
            return
        locator(record, "artifact_path")
        # Published snapshots normally contain no versions. A frozen draft may
        # also retain the known version-list/dictionary schema from its entity.
        versions = record.get("versions", [])
        entries = versions.values() if isinstance(versions, dict) else versions if isinstance(versions, list) else []
        for entry in entries:
            if isinstance(entry, dict):
                skill(entry.get("snapshot") or entry.get("data") or entry)

    if kind == "settings":
        locator(document, "artifact_dir")
    elif kind in {"evidence", "attachment", "material_read"}:
        locator(document, "path")
    elif kind == "skill":
        skill(document)
    elif kind == "run":
        evidence_records = document.get("evidence")
        for evidence in evidence_records if isinstance(evidence_records, list) else []:
            locator(evidence, "path")
        snapshot = document.get("config_snapshot")
        if isinstance(snapshot, dict):
            locator(snapshot.get("settings"), "artifact_dir")
            skill(snapshot.get("skill"))
            skill(snapshot.get("cmdb_skill"))
    return document


def restore_backup(store, artifact_dir, backup_dir, *, master_key):
    """Restore only into a fresh Store. No active sessions or old leases survive."""
    manifest, database = verify_backup(backup_dir, master_key=master_key)
    target, backup_root = Path(artifact_dir).resolve(), Path(backup_dir).resolve()
    source = backup_root / "artifacts"
    original_root = Path(manifest["source_artifact_root"]).resolve()
    if any(target == other or target.is_relative_to(other) or other.is_relative_to(target) for other in (backup_root, original_root)):
        raise BackupError("target_overlaps_backup_or_source")
    if target.exists() and any(target.iterdir()):
        raise BackupError("target_artifacts_not_empty")
    _assert_empty_target(store)
    if store.sqlite != (database.get("engine") == "sqlite"):
        raise BackupError("engine_mismatch")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = []
    try:
        for entry in manifest["files"]:
            output = target / entry["path"]
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source / entry["path"], output)
            output.chmod(entry["mode"] & 0o700)
            copied.append(output)
        def restore():
            # Recheck under the same database writer lock immediately before
            # any DELETE. A concurrent writer during file-copy preflight must
            # never have its data overwritten by a stale "empty" observation.
            _assert_empty_target(store)
            if {p for _, p in _safe_files(target)} != {entry["path"] for entry in manifest["files"]}:
                raise BackupError("target_artifacts_changed")
            for entry in manifest["files"]:
                file = target / entry["path"]
                h = hashlib.sha256()
                with file.open("rb") as reader:
                    while chunk := reader.read(1024 * 1024): h.update(chunk)
                if file.stat().st_size != entry["bytes"] or h.hexdigest() != entry["sha256"]:
                    raise BackupError("copied_artifact_integrity")
            if not store.sqlite and CHECKPOINT_TABLES.intersection(manifest["tables"]):
                from langgraph.checkpoint.postgres.base import BasePostgresSaver
                for migration in BasePostgresSaver.MIGRATIONS:
                    store._execute(migration.replace(" CONCURRENTLY", ""))
            old_namespace = database["namespace"]
            original_documents = {}
            for table in database["tables"]:
                name = table["name"]
                mapped = store.namespace + name[len(old_namespace):] if name.startswith(old_namespace + "_") else name
                allowed = {store._documents, store._events, store._worker_slot} | CHECKPOINT_TABLES
                if mapped not in allowed:
                    raise BackupError("unexpected_table")
                store._execute(f'DELETE FROM "{mapped}"')
                columns = table["columns"]
                if any(not re.fullmatch(r"[a-z_]+", c) for c in columns):
                    raise BackupError("unexpected_column")
                column_sql = ",".join('"' + c + '"' for c in columns)
                for values in table["rows"]:
                    values = [_decode(v) for v in values]
                    if mapped == store._documents:
                        item = json.loads(values[2]) if isinstance(values[2], str) else values[2]
                        original_documents[(values[0], values[1])] = copy.deepcopy(item)
                        if values[0] in {"auth_session", "session"}:
                            continue
                        item = _relocate_document(values[0], item, original_root, target)
                        if values[0] == "run" and item.get("status") in {"queued", "running"}:
                            item.update(status="failed", error={"code": "restore_interrupted", "message": "恢复不自动重放原请求"})
                        values[2] = store._pack(item)
                    elif not store.sqlite:
                        from psycopg.types.json import Jsonb
                        values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in values]
                    store._execute(f'INSERT INTO "{mapped}" ({column_sql}) VALUES ({",".join("?" for _ in columns)})', values)
            # A legacy archive hashes whole records, including file locators.
            # Verify the signed source records first, then bind only the target
            # location change without weakening normal archived() validation.
            from .legacy_archive import KIND, KEY, digest
            archive = store.get(KIND, KEY)
            if archive:
                if archive.get("schema") != "legacy-readonly-archive/v1" or archive.get("plan_digest") != digest(archive.get("entries")):
                    raise BackupError("legacy_archive_integrity")
                provenance = {"backup_signature": manifest["signature"], "source_plan_digest": archive["plan_digest"],
                    "source_entry_digests": []}
                for entry in archive["entries"]:
                    original = original_documents.get((entry["kind"], entry["id"]))
                    from .identity import resource_owner
                    if not original or resource_owner(original) != entry.get("owner_id") or digest(original) != entry.get("content_digest"):
                        raise BackupError("legacy_archive_integrity")
                    restored = store.get(entry["kind"], entry["id"])
                    if not restored or resource_owner(restored) != entry["owner_id"]:
                        raise BackupError("legacy_archive_integrity")
                    provenance["source_entry_digests"].append({"kind": entry["kind"], "id": entry["id"], "content_digest": entry["content_digest"]})
                    entry["content_digest"] = digest(restored)
                archive["plan_digest"] = digest(archive["entries"])
                archive.setdefault("restore_provenance", []).append(provenance)
                store.save(KIND, KEY, archive)
            _check_references(store, target, manifest["files"])
            store._execute(f"UPDATE {store._worker_slot} SET owner=NULL,run_id=NULL,expires_at=0,generation=generation+1 WHERE id=1")
            store.save("restore_guard", "main", {"verified": False, "restored_at": utcnow(), "reason": "latest_authorization_required", "backup_signature": manifest["signature"]})
        store.atomic(restore)
        return {"status": "restored_quarantined", "files": len(copied), "tables": len(database["tables"]), "latest_authorization_verified": False}
    except BaseException:
        for path in reversed(copied): path.unlink(missing_ok=True)
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_dir() and not any(path.iterdir()): path.rmdir()
        raise


def verify_restored_authorization(store, *, current_source_policy, current_egress_policy, revoked_secret_ids,
                                  disabled_user_ids, confirmed_by):
    """Explicit offline operator check against current external facts, not backup."""
    from .access_policy import PolicyService
    if not isinstance(confirmed_by, str) or not confirmed_by.strip() or not isinstance(revoked_secret_ids, list) or not isinstance(disabled_user_ids, list):
        raise BackupError("current_authorization_missing")
    guard = store.get("restore_guard", "main")
    if not guard or guard.get("verified"):
        raise BackupError("restore_guard_missing")
    PolicyService.validate_document("access_policy", current_source_policy)
    PolicyService.validate_document("egress_policy", current_egress_policy)
    def verify():
        store.save("access_policy", "main", copy.deepcopy(current_source_policy))
        store.save("egress_policy", "main", copy.deepcopy(current_egress_policy))
        for id in revoked_secret_ids:
            value = store.get("secret", id)
            if value: value.update(revoked=True); store.save("secret", id, value)
        for id in disabled_user_ids:
            value = store.get("user", id)
            if value: value.update(enabled=False); store.save("user", id, value)
        guard.update(verified=True, verified_at=utcnow(), confirmed_by=confirmed_by,
                     note="operator_supplied_current_facts; remote_account_validity_not_inferred")
        store.save("restore_guard", "main", guard)
    store.atomic(verify)
    return {"status": "authorization_verified"}
