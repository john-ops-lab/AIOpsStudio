"""Immutable, bounded saved-material reads. No source requests or arbitrary files."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


class ReadingError(ValueError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(detail)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def logical_document(native):
    """Return a documented virtual root, retaining its location in the saved bytes."""
    if isinstance(native, dict):
        if isinstance(native.get("structuredContent"), dict):
            return native["structuredContent"], "/structuredContent"
        content = native.get("content", [])
        if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
            try:
                value = json.loads(content[0]["text"])
                if isinstance(value, (dict, list)):
                    return value, "/content/0/text#json"
            except (ValueError, KeyError, TypeError):
                pass
    return native, ""


def pointer_get(value, pointer):
    if not isinstance(pointer, str) or len(pointer) > 1024 or (pointer and not pointer.startswith("/")):
        raise ReadingError("selector_invalid", "选择器必须是有界 JSON Pointer")
    if pointer.count("/") > 24:
        raise ReadingError("selector_invalid", "选择层级超过上限")
    for part in pointer.split("/")[1:] if pointer else []:
        if "~" in part.replace("~0", "").replace("~1", ""):
            raise ReadingError("selector_invalid", "JSON Pointer 转义无效")
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list):
                if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
                    raise KeyError()
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise KeyError()
        except (IndexError, KeyError):
            raise ReadingError("selector_missing", "所选位置不存在于已保存材料") from None
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ReadingError("selector_invalid", "读取位置或数量无效")
    return value


def select(value, selector, max_bytes=16384):
    allowed = {"pointer", "offset", "limit", "fields", "series", "time_start", "time_end", "time_field"}
    if not isinstance(selector, dict) or set(selector) - allowed:
        raise ReadingError("selector_invalid", "读取选择器包含不支持字段")
    pointer = selector.get("pointer", "")
    target = copy.deepcopy(pointer_get(value, pointer))
    # A series index is applied before row/time selection, and is part of the anchor.
    if "series" in selector:
        index = _integer(selector["series"], 0, 100000)
        if not isinstance(target, list) or index >= len(target):
            raise ReadingError("selector_missing", "所选序列不存在")
        target = target[index]
        pointer += "/" + str(index)
        if isinstance(target, dict) and "values" in target:
            target = target["values"]
            pointer += "/values"
    source_total = len(target) if isinstance(target, list) else None
    source_indices = range(source_total) if source_total is not None else None
    if "time_start" in selector or "time_end" in selector:
        if not isinstance(target, list) or not all(k in selector for k in ("time_start", "time_end")):
            raise ReadingError("selector_invalid", "时间选择必须指定完整绝对范围和行数组")
        def stamp(v):
            if isinstance(v, (float, int)) and not isinstance(v, bool):
                return datetime.fromtimestamp(v, timezone.utc).timestamp()
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if d.tzinfo is None:
                raise ValueError()
            return d.timestamp()
        try:
            lo, hi = stamp(selector["time_start"]), stamp(selector["time_end"])
            if lo > hi:
                raise ValueError()
            field = selector.get("time_field", "/0")
            source_indices = [index for index, row in enumerate(target) if lo <= stamp(pointer_get(row, field)) <= hi]
            target = [target[index] for index in source_indices]
        except (ValueError, TypeError, OverflowError, OSError):
            raise ReadingError("selector_invalid", "材料时间字段或绝对窗口无效") from None
    fields = selector.get("fields")
    if fields is not None and (not isinstance(fields, list) or not 1 <= len(fields) <= 24 or any(not isinstance(x, str) for x in fields)):
        raise ReadingError("selector_invalid", "字段选择必须是有界字段列表")
    if fields:
        def projection(row):
            if not isinstance(row, dict) or any(k not in row for k in fields):
                raise ReadingError("selector_missing", "所选字段不存在")
            return {k: row[k] for k in fields}
        target = [projection(row) for row in target] if isinstance(target, list) else projection(target)
    offset = _integer(selector.get("offset", 0), 0, 10000000)
    limit = _integer(selector.get("limit", 100), 1, 200)
    text = isinstance(target, str)
    if isinstance(target, (list, str)):
        total = len(target)
        if offset > total:
            raise ReadingError("selector_missing", "读取位置超出已保存范围")
        # One huge row is rejected: request a narrower field/string position instead.
        chunk = target[offset:offset + limit]
        while len(canonical(chunk).encode()) > max_bytes and len(chunk) > 1:
            chunk = chunk[:max(1, len(chunk) // 2)]
        if len(canonical(chunk).encode()) > max_bytes:
            raise ReadingError("selection_too_large", "单项超过阅读上限，请选择更小字段或字符范围")
        end = offset + len(chunk)
    else:
        if offset:
            raise ReadingError("selector_invalid", "对象或标量不支持偏移")
        total, end, chunk = 1, 1, target
        if len(canonical(chunk).encode()) > max_bytes:
            raise ReadingError("selection_too_large", "对象超过阅读上限，请选择字段或子数组")
    return {"data": chunk, "position": {"pointer": pointer, "offset": offset, "end": end,
            "total_saved_items": total, "unit": "characters" if text else "items",
            "source_indices": list(source_indices[offset:end]) if source_indices is not None else None,
            "source_total_items": source_total, "projection_fields": copy.deepcopy(fields) if fields else None},
            "has_more_saved": end < total, "next_offset": end if end < total else None,
            "selector": copy.deepcopy(selector)}


class MaterialReader:
    MODEL_LIMITS = {"calls": 16, "bytes": 131072, "result_bytes": 16384}
    USER_LIMITS = {"calls": 512, "bytes": 8388608, "result_bytes": 16384}

    def __init__(self, store, artifact_dir, *, authorize_material=None):
        self.store, self.artifact_dir = store, Path(artifact_dir).resolve()
        self.authorize_material = authorize_material

    def require_run(self, run_id, owner):
        run = self.store.get("run", run_id)
        if not run or (run.get("owner_id") or "admin") != owner:
            raise ReadingError("not_found", "材料不存在或不可访问")
        return run

    def native(self, record):
        try:
            run_id = record["run_id"]
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id):
                raise ValueError()
            supplied = Path(record["path"])
            base = self.artifact_dir / "evidence" / run_id
            path = supplied.resolve()
            if not supplied.is_absolute() or base.resolve() != base or path != supplied.absolute() or not path.is_relative_to(base):
                raise ValueError()
            # The final file cannot be a symlink/FIFO/device, and reading stays
            # bounded even if a writer changes its size between stat and read.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1048576:
                    raise ValueError()
                raw = source.read(8 * 1048576 + 1)
                if len(raw) > 8 * 1048576:
                    raise ValueError()
            if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                raise ValueError()
            return json.loads(raw)
        except (KeyError, OSError, ValueError, TypeError):
            raise ReadingError("material_unavailable", "材料已过期、不可读或完整性核对失败") from None

    def read(self, run_id, owner, request, *, consumer="user"):
        if consumer not in {"user", "model"} or not isinstance(request, dict):
            raise ReadingError("selector_invalid", "读取请求类型无效")
        run = self.require_run(run_id, owner)
        kind = request.get("resource_type")
        if kind not in {"evidence", "skill_reference"}:
            raise ReadingError("resource_type_invalid", "请明确选择证据或固定版本Skill资料")
        selector = request.get("selector", {})
        location = ""
        if kind == "evidence":
            allowed = {"resource_type", "evidence_id", "selector"}
            record = self.store.get("evidence", request.get("evidence_id", ""))
            if not record or record.get("run_id") != run_id or (record.get("owner_id") or "admin") != owner:
                raise ReadingError("not_found", "材料不存在或不可访问")
            if self.authorize_material:
                self.authorize_material(record, owner, run)
            native = self.native(record)
            value, location = logical_document(native)
            identity = {"evidence_id": record["id"], "sha256": record["sha256"]}
            source_info = record.get("coverage") or {"status": "unknown"}
            complete = not record.get("truncated")
        else:
            allowed = {"resource_type", "skill_id", "version_id", "path", "selector"}
            skill = next((s for s in (run.get("config_snapshot", {}).get("skill"), run.get("config_snapshot", {}).get("cmdb_skill"))
                          if s and s["id"] == request.get("skill_id") and s.get("version_id", "draft") == request.get("version_id")), None)
            path = request.get("path")
            if (not skill or not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
                    or ".." in PurePosixPath(path).parts or "\\" in path or path not in skill.get("references", {})):
                raise ReadingError("not_found", "材料不存在或不可访问")
            current = self.store.get("skill", skill["id"])
            if current and current.get("blocked"):
                raise ReadingError("not_found", "材料不存在或不可访问")
            value = skill["references"][path]
            if not isinstance(value, str):
                raise ReadingError("material_unavailable", "固定资料类型无效")
            # Text defaults to character slicing; callers can select exact line slices.
            value = {"text": value, "lines": value.splitlines()}
            identity = {"skill_id": skill["id"], "version_id": skill.get("version_id", "draft"), "path": path,
                        "content_digest": fingerprint(value)}
            source_info, complete = {"status": "skill_reference_not_source_evidence"}, True
        if set(request) - allowed:
            raise ReadingError("selector_invalid", "读取请求包含不支持字段")
        result = select(value, selector, (self.MODEL_LIMITS if consumer == "model" else self.USER_LIMITS)["result_bytes"])
        anchor_data = {"resource_type": kind, **identity, "location": location, "selector": selector, "position": result["position"], "data_digest": fingerprint(result["data"])}
        anchor_id = "frag_" + fingerprint(anchor_data)[:32]
        result.update(resource_type=kind, anchor_id=anchor_id, material=identity, saved_root=location,
                      completeness={"source": source_info, "storage_complete": complete,
                                    "selection_complete": not result["has_more_saved"],
                                    "notice": "仅覆盖实际保存材料；源未取得或保存时丢弃的数据不能由补读恢复"})
        def account():
            latest = self.require_run(run_id, owner)
            if kind == "evidence" and self.authorize_material:
                self.authorize_material(record, owner, latest)
            limits = latest.get("reading_limits", {}).get(consumer) or (self.MODEL_LIMITS if consumer == "model" else self.USER_LIMITS)
            meters = copy.deepcopy(latest.get("reading_usage", {})); meter = meters.setdefault(consumer, {"calls": 0, "bytes": 0, "estimated_tokens": 0, "repeated_reads": 0})
            if meter["calls"] >= limits["calls"]:
                raise ReadingError("reading_budget", "本轮材料读取预算已耗尽，未宣称完整覆盖")
            key = "read_" + fingerprint({"run_id": run_id, "consumer": consumer, "anchor_id": anchor_id})[:32]
            previous = self.store.get("material_read", key)
            before = copy.deepcopy(meter)
            meter.update(calls=before["calls"] + 1, repeated_reads=before["repeated_reads"] + bool(previous))
            # The accounting fields themselves change the response's size. The
            # monotone byte/token digit lengths converge to an exact fixed point;
            # no metadata or nested usage structure is exempt from the budget.
            for _ in range(32):
                result["usage"] = copy.deepcopy(meter)
                byte_count = len(canonical(result).encode())
                next_bytes = before["bytes"] + byte_count
                next_tokens = before["estimated_tokens"] + (byte_count + 1) // 2
                if meter["bytes"] == next_bytes and meter["estimated_tokens"] == next_tokens:
                    break
                meter.update(bytes=next_bytes, estimated_tokens=next_tokens)
            else:
                raise ReadingError("selection_too_large", "读取回执计量无法收敛，请缩小选择范围")
            packet_limit = (self.MODEL_LIMITS if consumer == "model" else self.USER_LIMITS)["result_bytes"]
            if byte_count > packet_limit:
                raise ReadingError("selection_too_large", "完整阅读回执超过上限，请选择更小字段或范围")
            if next_bytes > limits["bytes"]:
                raise ReadingError("reading_budget", "本轮材料读取预算已耗尽，未宣称完整覆盖")
            self.store.save("material_read", key, {"owner_id": owner, "run_id": run_id, "attempt_id": run["attempt_id"], "consumer": consumer,
                "anchor_id": anchor_id, "anchor": anchor_data, "result": copy.deepcopy(result), "calls": (previous or {}).get("calls", 0) + 1, "bytes": byte_count})
            latest["reading_usage"] = meters
            self.store.save("run", run_id, latest, latest["revision"])
            self.store.event(run_id, "material_read", {"resource_type": kind, "anchor_id": anchor_id, "consumer": consumer, "bytes": byte_count, "storage_complete": complete})
            return meters[consumer]
        result["usage"] = self.store.atomic(account)
        return result
