"""Real temporary bytes and SQLite receipts; synthetic material, no live sources."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from aiopsstudio.material_reading import MaterialReader, ReadingError, canonical, fingerprint, logical_document, select
from aiopsstudio.store import Store


@pytest.fixture
def material(tmp_path):
    store = Store("sqlite:///:memory:")
    store.save("run", "run_fixture", {"owner_id": "alice", "attempt_id": "attempt_fixture", "config_snapshot": {
        "skill": {"id": "skill_fixture", "version_id": "published-1", "references": {"references/guide.txt": "first\nsecond\nthird"}}}})
    reader = MaterialReader(store, tmp_path)
    def save(value, *, id="ev_fixture", run_id="run_fixture", owner="alice", **extra):
        path = tmp_path / "evidence" / run_id / (id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, ensure_ascii=False).encode()
        path.write_bytes(raw)
        return store.save("evidence", id, {"run_id": run_id, "owner_id": owner, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "coverage": {"status": "partial"}, **extra})
    yield store, reader, save
    store.close()


def request(selector=None, id="ev_fixture"):
    return {"resource_type": "evidence", "evidence_id": id, "selector": selector or {}}


def assert_code(code, function, *args, **kwargs):
    with pytest.raises(ReadingError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == code


@pytest.mark.parametrize("native", [None, {"content": None}, {"content": "text"}, {"content": 12}, {"content": {}}, {"content": [None]}])
def test_logical_root_handles_null_and_non_list_content(native):
    assert logical_document(native) == (native, "")


def test_documented_virtual_roots_preserve_saved_location():
    assert logical_document({"structuredContent": {"rows": [1]}}) == ({"rows": [1]}, "/structuredContent")
    assert logical_document({"content": [{"type": "text", "text": '[1,2]'}]}) == ([1, 2], "/content/0/text#json")
    native = {"content": [{"type": "text", "text": "not JSON"}]}
    assert logical_document(native) == (native, "")


def test_real_bytes_hash_and_saved_result_are_immutable(material):
    store, reader, save = material
    record = save({"structuredContent": {"rows": [{"v": 1}, {"v": 2}]}}, truncated=True)
    before = Path(record["path"]).read_bytes()
    result = reader.read("run_fixture", "alice", request({"pointer": "/rows", "offset": 0, "limit": 1}))
    assert result["data"] == [{"v": 1}]
    assert result["saved_root"] == "/structuredContent" and result["next_offset"] == 1
    assert result["completeness"]["storage_complete"] is False
    receipt = store.list("material_read")[0]
    assert receipt["result"] == result and receipt["anchor"]["data_digest"] == fingerprint(result["data"])
    assert "path" not in receipt["result"] and record["path"] not in json.dumps(receipt["result"])
    result["data"][0]["v"] = "client changed returned object"
    result["usage"]["calls"] = 999
    assert store.list("material_read")[0]["result"]["data"] == [{"v": 1}]
    assert store.list("material_read")[0]["result"]["usage"]["calls"] == 1
    assert Path(record["path"]).read_bytes() == before
    Path(record["path"]).write_bytes(b'{"tampered":true}')
    assert_code("material_unavailable", reader.read, "run_fixture", "alice", request())
    assert store.list("material_read")[0]["calls"] == 1


def test_repeat_anchor_stays_stable_while_reading_is_metered(material):
    store, reader, save = material
    save([1, 2, 3])
    first = reader.read("run_fixture", "alice", request({"offset": 1, "limit": 1}), consumer="model")
    second = reader.read("run_fixture", "alice", request({"offset": 1, "limit": 1}), consumer="model")
    assert first["anchor_id"] == second["anchor_id"]
    assert first["usage"]["calls"] == 1 and second["usage"]["calls"] == 2
    assert second["usage"]["repeated_reads"] == 1
    assert len(store.list("material_read")) == 1
    assert store.list("material_read")[0]["result"] == second
    changed = reader.read("run_fixture", "alice", request({"offset": 2, "limit": 1}), consumer="model")
    assert changed["anchor_id"] != first["anchor_id"]


def test_run_evidence_and_consumer_ownership_are_not_client_fields(material):
    store, reader, save = material
    save([1])
    assert_code("not_found", reader.read, "run_fixture", "bob", request())
    save([2], id="ev_other", owner="bob")
    assert_code("not_found", reader.read, "run_fixture", "alice", request(id="ev_other"))
    save([3], id="ev_wrong_run", run_id="run_other")
    assert_code("not_found", reader.read, "run_fixture", "alice", request(id="ev_wrong_run"))
    assert_code("selector_invalid", reader.read, "run_fixture", "alice", {**request(), "owner_id": "alice"})
    assert_code("selector_invalid", reader.read, "run_fixture", "alice", request(), consumer="admin")
    assert store.list("material_read") == []


def test_latest_material_authorization_is_rechecked_before_receipt(material):
    store, reader, save = material
    save([1, 2])
    calls = []
    def permission(record, owner, run):
        calls.append((record["id"], owner, run["id"]))
        if len(calls) == 2:
            raise ReadingError("revoked", "synthetic revocation before commit")
    reader.authorize_material = permission
    assert_code("revoked", reader.read, "run_fixture", "alice", request())
    assert len(calls) == 2 and store.list("material_read") == []
    assert not store.get("run", "run_fixture").get("reading_usage")


def test_select_series_time_and_projection_are_composable():
    data = {"series": [{"values": [[1, 0], [2, 10]]}, {"values": [[1, 3], [2, 42], [3, 0]]}]}
    result = select(data, {"pointer": "/series", "series": 1, "time_start": "1970-01-01T00:00:02Z", "time_end": "1970-01-01T00:00:03Z", "offset": 0, "limit": 1})
    assert result["data"] == [[2, 42]] and result["position"]["pointer"] == "/series/1/values"
    assert result["has_more_saved"] is True and result["next_offset"] == 1
    rows = [{"at": "2026-01-01T10:00:00Z", "count": 0, "secret_noise": "not selected"}, {"at": "2026-01-01T11:00:00Z", "count": 2}]
    result = select(rows, {"time_field": "/at", "time_start": "2026-01-01T10:00:00+00:00", "time_end": "2026-01-01T10:00:00Z", "fields": ["count"]})
    assert result["data"] == [{"count": 0}]


@pytest.mark.parametrize("selector,code", [
    ({"pointer": "relative"}, "selector_invalid"), ({"pointer": "/bad~2"}, "selector_invalid"),
    ({"pointer": "/missing"}, "selector_missing"), ({"offset": True}, "selector_invalid"),
    ({"limit": 0}, "selector_invalid"), ({"limit": 201}, "selector_invalid"),
    ({"offset": 999}, "selector_missing"), ({"series": 99}, "selector_missing"),
    ({"time_start": "now", "time_end": "2026-01-01T00:00:00Z"}, "selector_invalid"),
    ({"time_start": "2026-01-01T00:00:00Z"}, "selector_invalid"),
    ({"fields": ["missing"]}, "selector_missing"), ({"fields": []}, "selector_invalid"),
    ({"url": "https://untrusted.invalid"}, "selector_invalid"),
])
def test_invalid_selection_never_creates_a_receipt(material, selector, code):
    store, reader, save = material
    save([{"count": 1}])
    assert_code(code, reader.read, "run_fixture", "alice", request(selector))
    assert store.list("material_read") == []


def test_pointer_escaping_and_scalar_zero_are_preserved():
    assert select({"a/b": {"x~y": 0}}, {"pointer": "/a~1b/x~0y"})["data"] == 0
    assert select([[], [False]], {"pointer": "/1/0"})["data"] is False
    assert_code("selector_missing", select, [1], {"pointer": "/01"})
    assert_code("selector_invalid", select, 0, {"offset": 1})


def test_invalid_json_with_matching_hash_still_cannot_be_read(material):
    store, reader, save = material
    record = save({"safe": True})
    raw = b"not-json-but-real-frozen-bytes"
    Path(record["path"]).write_bytes(raw)
    record["sha256"] = hashlib.sha256(raw).hexdigest()
    store.save("evidence", record["id"], record)
    assert_code("material_unavailable", reader.read, "run_fixture", "alice", request())
    assert store.list("material_read") == []


def test_run_id_traversal_cannot_expand_the_file_base(material):
    store, reader, save = material
    record = save({"safe": True})
    for run_id in ("../run_fixture", "/run_fixture", "..", "run/fixture"):
        assert_code("material_unavailable", reader.native, {**record, "run_id": run_id})


def test_utf8_byte_limit_shrinks_pages_and_single_large_item_fails():
    result = select(["中" * 20] * 8, {"limit": 8}, max_bytes=150)
    assert len(result["data"]) == 2 and result["has_more_saved"]
    assert_code("selection_too_large", select, ["中" * 100], {}, max_bytes=150)
    result = select("中" * 100, {"limit": 100}, max_bytes=150)
    assert result["position"]["unit"] == "characters" and result["has_more_saved"]
    assert len(json.dumps(result["data"], ensure_ascii=False).encode()) <= 150


@pytest.mark.parametrize("kind", ["outside", "relative", "symlink_file", "symlink_directory", "fifo", "missing", "oversize"])
def test_material_paths_are_bounded_regular_files_inside_the_run(material, tmp_path, kind):
    store, reader, save = material
    record = save({"safe": True})
    if kind == "outside":
        path = tmp_path / "outside.json"; path.write_bytes(Path(record["path"]).read_bytes())
    elif kind == "relative":
        path = Path(os.path.relpath(record["path"], Path.cwd()))
    elif kind == "symlink_file":
        path = Path(record["path"]).with_name("link.json"); path.symlink_to(record["path"])
    elif kind == "symlink_directory":
        base = Path(record["path"]).parent; base.rename(base.with_name("saved")); base.symlink_to(base.with_name("saved"), target_is_directory=True); path = Path(record["path"])
    elif kind == "fifo":
        path = Path(record["path"]).with_name("pipe"); os.mkfifo(path)
    elif kind == "missing":
        path = Path(record["path"]).with_name("missing.json")
    else:
        path = Path(record["path"]); path.write_bytes(b" " * (8 * 1048576 + 1))
    record.update(path=str(path))
    store.save("evidence", record["id"], record)
    assert_code("material_unavailable", reader.read, "run_fixture", "alice", request())
    assert store.list("material_read") == []


def test_skill_reference_uses_exact_frozen_version_not_latest_draft(material):
    store, reader, _ = material
    store.save("skill", "skill_fixture", {"references": {"references/guide.txt": "new changed text"}})
    req = {"resource_type": "skill_reference", "skill_id": "skill_fixture", "version_id": "published-1", "path": "references/guide.txt", "selector": {"pointer": "/lines", "offset": 1, "limit": 1}}
    result = reader.read("run_fixture", "alice", req)
    assert result["data"] == ["second"] and result["material"]["version_id"] == "published-1"
    assert result["completeness"]["source"]["status"] == "skill_reference_not_source_evidence"
    assert_code("not_found", reader.read, "run_fixture", "alice", {**req, "version_id": "draft"})
    for path in ("../private", "/etc/passwd", "references\\private", "references/missing"):
        assert_code("not_found", reader.read, "run_fixture", "alice", {**req, "path": path})
    current = store.get("skill", "skill_fixture"); current["blocked"] = True; store.save("skill", "skill_fixture", current)
    assert_code("not_found", reader.read, "run_fixture", "alice", req)


@pytest.mark.parametrize("limit", [{"calls": 1, "bytes": 100000}, {"calls": 10, "bytes": 2}])
def test_user_and_model_quotas_fail_without_recording_a_read(material, limit):
    store, reader, save = material
    save([1, 2])
    run = store.get("run", "run_fixture"); run["reading_limits"] = {"user": limit}; store.save("run", "run_fixture", run)
    if limit["bytes"] > 2:
        reader.read("run_fixture", "alice", request())
    before = copy.deepcopy(store.list("material_read"))
    assert_code("reading_budget", reader.read, "run_fixture", "alice", request())
    assert store.list("material_read") == before
    # A separate model meter does not consume the exhausted human meter.
    model = reader.read("run_fixture", "alice", request(), consumer="model")
    assert model["usage"]["calls"] == 1


def test_full_packet_bytes_and_usage_reach_exact_non_recursive_fixed_point(material):
    store, reader, save = material
    save({"rows": ["中" * 10]})
    first = reader.read("run_fixture", "alice", request({"pointer": "/rows"}))
    first_size = len(canonical(first).encode())
    assert first["usage"]["bytes"] == first_size
    assert first["usage"]["estimated_tokens"] == (first_size + 1) // 2
    second = reader.read("run_fixture", "alice", request({"pointer": "/rows"}))
    second_size = len(canonical(second).encode())
    assert second["usage"]["bytes"] == first_size + second_size
    assert second["usage"]["estimated_tokens"] == (first_size + 1) // 2 + (second_size + 1) // 2
    assert "usage" not in second["usage"]
    assert store.list("material_read")[0]["bytes"] == second_size


@pytest.mark.parametrize("metadata", [False, True])
def test_metadata_and_data_both_count_toward_single_packet_limit(material, metadata):
    store, reader, save = material
    if metadata:
        save([1], coverage={"status": "unknown", "binding": {"large_metadata": "中" * 6000}})
    else:
        save(["x" * 16000])
    assert_code("selection_too_large", reader.read, "run_fixture", "alice", request())
    assert store.list("material_read") == []
    assert not store.get("run", "run_fixture").get("reading_usage")


def test_small_data_cannot_evade_full_packet_byte_quota(material):
    store, reader, save = material
    save([0])
    run = store.get("run", "run_fixture"); run["reading_limits"] = {"model": {"calls": 10, "bytes": 100}}; store.save("run", run["id"], run)
    assert_code("reading_budget", reader.read, "run_fixture", "alice", request(), consumer="model")
    assert store.list("material_read") == []


def test_original_indices_survive_time_filter_projection_and_byte_page_shrink():
    rows = [{"at": i, "public": "中" * 20, "private": "omitted"} for i in range(10)]
    result = select({"rows": rows}, {"pointer": "/rows", "time_field": "/at", "time_start": "1970-01-01T00:00:04Z", "time_end": "1970-01-01T00:00:09Z", "fields": ["public"], "offset": 1, "limit": 4}, max_bytes=200)
    assert result["position"]["source_indices"] == [5, 6]
    assert result["position"]["source_total_items"] == 10
    assert result["position"]["total_saved_items"] == 6
    assert result["position"]["projection_fields"] == ["public"]
    assert result["next_offset"] == 3
