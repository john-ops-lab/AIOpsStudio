"""Restore relocates declared locators without rewriting opaque business JSON."""
import copy
import hashlib
import json
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from aiopsstudio.backup import create_backup, restore_backup
from aiopsstudio.config import ConfigService, Vault
from aiopsstudio.store import Store


@pytest.mark.parametrize("dictionary_versions", [False, True])
def test_restore_preserves_business_path_keys_and_relocates_declared_locators(tmp_path, dictionary_versions):
    source = Store("sqlite:///" + str(tmp_path / "source.db"))
    target = Store("sqlite:///" + str(tmp_path / "target.db"))
    try:
        root = (tmp_path / "assets").resolve()
        key = Fernet.generate_key()
        config = ConfigService(source, Vault(source, key), root)
        literal = str(root / "business-value")
        business = {"path": literal, "artifact_dir": literal, "artifact_path": literal,
                    "nested": [{"path": literal}], "text": literal}
        skill = config.save("skill", {"name": "opaque-path-fixture",
            "content": "---\nname: opaque-path-fixture\ndescription: synthetic\n---\nFixed method.",
            "references": {"references/a.txt": literal}})
        skill = config.publish("skill", skill["id"], skill["revision"], channel="experimental")
        snapshot = skill["versions"][0]["snapshot"]
        package = Path(snapshot["artifact_path"])
        if dictionary_versions:
            skill["versions"] = {v["id"]: v for v in skill["versions"]}
            skill = source.save("skill", skill["id"], skill)
        evidence_file = root / "evidence.json"
        evidence_file.write_text(json.dumps(business))
        raw = evidence_file.read_bytes()
        evidence = source.save("evidence", "evidence-one", {"path": str(evidence_file),
            "sha256": hashlib.sha256(raw).hexdigest(), "owner_id": "one", "business": business})
        read = source.save("material_read", "read-one", {"path": str(evidence_file),
            "result": {"data": business}, "anchor": {"data": business}})
        settings = source.get("settings", "main")
        settings["query_catalog"] = copy.deepcopy(business)
        settings = source.save("settings", "main", settings)
        frozen_skill = copy.deepcopy(snapshot)
        frozen_skill["versions"] = copy.deepcopy(skill["versions"])
        run = source.save("run", "run-one", {"status": "partial", "owner_id": "one",
            "inputs": business, "report": {"analysis": literal, "business": business},
            "history_context": {"rounds": [business]}, "_request": {"inputs": business},
            "user_materials": [business], "evidence": [evidence],
            "config_snapshot": {"settings": settings, "skill": frozen_skill,
                "cmdb_skill": snapshot, "model": {"extra_body": business}}})
        opaque = source.save("unrecognized-business-document", "opaque-one", business)
        before = {kind: source.list(kind) for kind in ("settings", "skill", "evidence", "material_read", "run", "unrecognized-business-document")}
        create_backup(source, root, tmp_path / "backup", master_key=key)
        out = tmp_path / "restored"
        restore_backup(target, out, tmp_path / "backup", master_key=key)

        restored = target.get("run", "run-one")
        for name in ("inputs", "report", "history_context", "_request", "user_materials"):
            assert restored[name] == run[name], name
        assert target.get("material_read", "read-one")["result"] == read["result"]
        assert target.get("material_read", "read-one")["anchor"] == read["anchor"]
        assert target.get("evidence", "evidence-one")["business"] == business
        assert target.get("settings", "main")["query_catalog"] == business
        assert restored["config_snapshot"]["model"]["extra_body"] == business
        assert target.get("unrecognized-business-document", "opaque-one") == opaque

        expected_file = str(out / evidence_file.relative_to(root))
        for kind, record_id in (("evidence", "evidence-one"), ("material_read", "read-one")):
            assert target.get(kind, record_id)["path"] == expected_file
        assert restored["evidence"][0]["path"] == expected_file
        assert restored["evidence"][0]["business"] == business
        assert target.get("settings", "main")["artifact_dir"] == str(out)
        assert restored["config_snapshot"]["settings"]["artifact_dir"] == str(out)
        assert restored["config_snapshot"]["settings"]["query_catalog"] == business
        expected_package = str(out / package.relative_to(root))
        global_versions = target.get("skill", skill["id"])["versions"]
        first_version = next(iter(global_versions.values())) if dictionary_versions else global_versions[0]
        for item in (restored["config_snapshot"]["skill"], restored["config_snapshot"]["cmdb_skill"],
                     first_version["snapshot"]):
            assert item["artifact_path"] == expected_package
            assert item["references"] == snapshot["references"]
        frozen_versions = restored["config_snapshot"]["skill"]["versions"]
        first_frozen = next(iter(frozen_versions.values())) if dictionary_versions else frozen_versions[0]
        assert first_frozen["snapshot"]["artifact_path"] == expected_package
        assert (out / "evidence.json").read_bytes() == raw
        assert {kind: source.list(kind) for kind in before} == before
    finally:
        source.close()
        target.close()
