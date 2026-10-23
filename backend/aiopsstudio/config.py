"""Configuration lifecycle, immutable artifacts, and server-only encrypted secrets."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from jsonschema import Draft202012Validator
import yaml

from .store import Store, StoreConflict, utcnow


class ConfigError(ValueError):
    def __init__(self, detail: str, code: str = "invalid_configuration"):
        self.detail, self.code = detail, code
        super().__init__(detail)


class Vault:
    def __init__(self, store: Store, master_key: str | bytes):
        self.store = store
        try:
            self.cipher = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)
        except Exception:
            raise ValueError("独立主密钥格式无效，必须为 Fernet 密钥") from None

    def put(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 65536:
            raise ConfigError("凭据不能为空或超过 64 KiB")
        id = "sec_" + secrets.token_hex(16)
        self.store.save("secret", id, {"ciphertext": self.cipher.encrypt(value.encode()).decode(), "revoked": False})
        return id

    def get(self, secret_id: str) -> str:
        record = self.store.get("secret", secret_id)
        if not record or record.get("revoked"):
            raise ConfigError("凭据不存在或已撤销", "secret_revoked")
        try:
            return self.cipher.decrypt(record["ciphertext"].encode()).decode()
        except (InvalidToken, KeyError):
            raise ConfigError("凭据无法解密，请检查独立主密钥", "secret_decryption_failed") from None

    def revoke(self, secret_id: str):
        record = self.store.get("secret", secret_id)
        if not record:
            raise ConfigError("凭据不存在")
        record.update(revoked=True)
        self.store.save("secret", secret_id, record)

    def sensitive_values(self) -> list[str]:
        values = []
        for record in self.store.list("secret"):
            try:
                values.append(self.cipher.decrypt(record["ciphertext"].encode()).decode())
            except (InvalidToken, KeyError):
                continue
        return values


_SENSITIVE_KEYS = {"secret_value", "password", "api_key", "access_token", "refresh_token", "authorization", "ciphertext", "master_key", "cookie", "set-cookie", "password_hash", "session_hash", "x-api-key", "x-auth-token", "api-key", "secret", "client_secret"}


def redact(data, vault: Vault | None = None):
    """Remove secret-bearing fields, then scrub known credentials even inside errors."""
    values = vault.sensitive_values() if vault else []
    def walk(value):
        if isinstance(value, dict):
            return {str(k): walk(v) for k, v in value.items() if str(k).lower() not in _SENSITIVE_KEYS and not str(k).lower().endswith("_secret_value")}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str):
            for secret_value in values:
                if secret_value:
                    value = value.replace(secret_value, "[已脱敏]")
            value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[已脱敏]", value)
            return value
        return value
    return walk(data)


def read_private_file(path: str, label: str) -> str:
    source = Path(path)
    if source.stat().st_mode & 0o077:
        raise ValueError(f"{label}文件权限必须为 0600 或更严格")
    return source.read_text().strip()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_url(value: str, label: str):
    try:
        parts = urlparse(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.fragment or parts.query:
            raise ValueError()
        _ = parts.port
    except ValueError:
        raise ConfigError(f"{label}必须为不含凭据、查询参数和片段的 HTTP(S) 地址") from None


class ConfigService:
    def __init__(self, store: Store, vault: Vault, artifact_dir: str | Path):
        self.store, self.vault = store, vault
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if not store.get("settings", "main"):
            store.save("settings", "main", {"default_model_id": None, "budget": {"tool_calls": 40, "source_requests": 40, "model_calls": 24, "wall_seconds": 300, "max_tokens": 100000, "evidence_bytes": 1048576}, "queue_limit": 10, "retention_days": 7, "evidence_limit_bytes": 1048576, "storage_quota_bytes": 1073741824, "ongrid": {}, "environments": {}, "identity_mapping": {}, "artifact_dir": str(self.artifact_dir)})

    def audit(self, action: str, kind: str, id: str, details=None, actor="admin"):
        aid = "audit_" + secrets.token_hex(12)
        return self.store.save("audit", aid, {"actor": actor, "action": action, "kind": kind, "entity_id": id, "details": redact(details or {}, self.vault), "time": utcnow()})

    def require(self, kind: str, id: str):
        item = self.store.get(kind, id)
        if not item:
            raise ConfigError("配置不存在", "not_found")
        return item

    def _secret(self, payload: dict, previous: dict):
        if payload.get("secret_value"):
            return self.vault.put(payload["secret_value"])
        value = payload.get("secret_id", previous.get("secret_id"))
        if value:
            self.vault.get(value)
        return value

    def save(self, kind: str, payload: dict, id: str | None = None):
        def write():
            previous = self.require(kind, id) if id else {}
            if id and "expected_revision" not in payload:
                raise ConfigError("保存必须提供 expected_revision", "revision_required")
            entity_id = id or kind + "_" + secrets.token_hex(10)
            if id and payload.get("expected_revision") != previous["revision"]:
                raise StoreConflict("配置已被其他页面修改，请重新读取后保存")
            base = copy.deepcopy(previous)
            defaults = {
                "skill": {"name": "", "display_name": "", "description": "", "content": "", "references": {}, "input_schema": {"type": "object"}, "dependencies": []},
                "mcp": {"name": "", "url": "", "auth_type": "none", "secret_id": None, "auth_header": "Authorization", "timeout_seconds": 30, "allowed_tools": [], "tools": [], "transport": "streamable_http", "ca_file": None, "tls_ca_file": None, "headers": {}, "verify_tls": True, "max_response_bytes": 1048576},
                "model": {"name": "", "provider": "openai", "model": "", "base_url": "https://api.openai.com/v1", "secret_id": None, "temperature": None, "max_tokens": 2048, "extra_body": {}, "default_headers": {}},
            }[kind]
            fields = set(defaults)
            base = {**defaults, **base}
            for field in fields:
                if field in payload:
                    base[field] = copy.deepcopy(payload[field])
            if kind in {"mcp", "model"}:
                base["secret_id"] = self._secret(payload, previous)
            base.setdefault("enabled", False)
            base.setdefault("blocked", False)
            base.setdefault("current_version", None)
            base.setdefault("versions", [])
            if kind == "mcp" and previous.get("candidate_tools") and "tools" in payload:
                catalog = {t["name"]: t for t in previous["candidate_tools"]}
                for tool in base["tools"]:
                    original = catalog.get(tool.get("name"))
                    if original is None or any(tool.get(k) != original.get(k) for k in ("name", "inputSchema", "outputSchema")):
                        raise ConfigError("不能改写已发现工具定义，请重新发现目录")
            if kind == "mcp" and previous and any(base.get(k) != previous.get(k) for k in ("url", "auth_type", "auth_header", "secret_id", "ca_file", "tls_ca_file", "headers")):
                base["discovery"] = {"complete": False, "reason": "connection_changed", "checked_at": utcnow()}
            base["status"] = "draft"
            base.pop("validation", None)
            self._basic(kind, base)
            saved = self.store.save(kind, entity_id, base, payload.get("expected_revision", 0))
            self.audit("create" if not id else "save", kind, entity_id, {"revision": saved["revision"], "changed_fields": sorted(set(payload) - {"secret_value"})})
            return saved
        return self.store.atomic(write)

    def _basic(self, kind, item):
        if not isinstance(item.get("name"), str) or not item["name"].strip() or len(item["name"]) > 128:
            raise ConfigError("名称不能为空或超过 128 个字符")
        if kind == "skill":
            if not isinstance(item["content"], str) or len(item["content"].encode()) > 262144:
                raise ConfigError("Skill 正文必须为不超过 256 KiB 的文本")
            refs = item["references"]
            if not isinstance(refs, dict) or len(refs) > 32:
                raise ConfigError("参考文件必须为字典且不超过 32 个")
            for path, content in refs.items():
                p = PurePosixPath(path)
                if not path.startswith("references/") or p.is_absolute() or ".." in p.parts or "\\" in path or not isinstance(content, str) or len(content.encode()) > 262144:
                    raise ConfigError("参考文件路径必须位于 references/ 内且为不超过 256 KiB 的文本")
            if sum(len(c.encode()) for c in refs.values()) > 1048576:
                raise ConfigError("参考文件总大小不能超过 1 MiB")
            if not isinstance(item["dependencies"], list):
                raise ConfigError("依赖必须为列表")
            try:
                Draft202012Validator.check_schema(item["input_schema"])
            except Exception:
                raise ConfigError("输入 JSON Schema 无效") from None
        elif kind == "mcp":
            validate_url(item["url"], "MCP 地址")
            if item["auth_type"] not in {"none", "bearer", "header", "custom_header"}:
                raise ConfigError("首版只支持无认证、Bearer 或指定认证头")
            if item["transport"] != "streamable_http":
                raise ConfigError("首版只支持 Streamable HTTP")
            if not isinstance(item["timeout_seconds"], (int, float)) or not 1 <= item["timeout_seconds"] <= 120:
                raise ConfigError("超时必须在 1 至 120 秒之间")
            if not isinstance(item["allowed_tools"], list) or any(not isinstance(v, str) for v in item["allowed_tools"]):
                raise ConfigError("工具白名单必须为名称列表")
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", item["auth_header"]):
                raise ConfigError("认证头名称无效")
            if item["auth_header"].lower() in {"host", "cookie", "content-length", "content-type", "connection", "transfer-encoding"}:
                raise ConfigError("该请求头不能用于认证")
            if item.get("verify_tls") is not True:
                raise ConfigError("不允许关闭 TLS 证书校验")
            for field in ("ca_file", "tls_ca_file"):
                if item.get(field) and (not isinstance(item[field], str) or not Path(item[field]).is_file()):
                    raise ConfigError("CA 证书文件不存在")
            headers = item.get("headers", {})
            if not isinstance(headers, dict) or any(k.lower() not in {"host", "accept"} or not isinstance(v, str) or "\r" in v or "\n" in v for k, v in headers.items()):
                raise ConfigError("MCP 附加请求头只支持 Host 与 Accept")
            if any(k.lower() == "host" for k in headers) and urlparse(item["url"]).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ConfigError("Host 请求头覆盖仅允许本机回环连接")
            if not isinstance(item.get("max_response_bytes"), int) or not 1024 <= item["max_response_bytes"] <= 10485760:
                raise ConfigError("响应保存上限必须在 1 KiB 至 10 MiB 之间")
            if not isinstance(item["tools"], list) or any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in item["tools"]):
                raise ConfigError("工具目录必须为含名称的对象列表")
            for tool in item["tools"]:
                try:
                    Draft202012Validator.check_schema(tool.get("inputSchema", {"type": "object"}))
                except Exception:
                    raise ConfigError("工具输入 JSON Schema 无效") from None
        else:
            validate_url(item["base_url"], "模型地址")
            if item["provider"] not in {"openai", "openai_compatible", "openai-compatible", "kimi", "moonshot"}:
                raise ConfigError("模型 Provider 不受支持")
            if not isinstance(item["model"], str) or not item["model"].strip():
                raise ConfigError("模型 ID 不能为空")
            if not isinstance(item["max_tokens"], int) or not 1 <= item["max_tokens"] <= 32768:
                raise ConfigError("最大输出必须在 1 至 32768 token 之间")
            if not isinstance(item.get("extra_body"), dict) or not isinstance(item.get("default_headers"), dict):
                raise ConfigError("模型附加参数与请求头必须为对象")
            self._reject_embedded_secrets(item["extra_body"])
            for key, value in item["default_headers"].items():
                if not isinstance(value, str) or "\r" in value or "\n" in value or key.lower() in _SENSITIVE_KEYS | {"host", "connection", "content-length"}:
                    raise ConfigError("模型附加请求头无效，认证必须使用凭据引用")
            temp = item.get("temperature")
            if temp is not None and (not isinstance(temp, (int, float)) or not 0 <= temp <= 2):
                raise ConfigError("temperature 必须为空或 0 至 2")

    def validate(self, kind, item):
        errors, warnings = [], []
        try:
            self._basic(kind, item)
            if item.get("secret_id"):
                self.vault.get(item["secret_id"])
            if kind == "skill":
                content = item["content"]
                if not content.startswith("---\n") or "\n---" not in content[4:]:
                    raise ConfigError("SKILL.md 必须包含 YAML frontmatter")
                _, frontmatter, body = content.split("---", 2)
                metadata = yaml.safe_load(frontmatter)
                if not isinstance(metadata, dict) or not metadata.get("name") or not metadata.get("description") or not body.strip():
                    raise ConfigError("frontmatter 必须包含 name、description，正文不能为空")
                if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(metadata["name"])):
                    raise ConfigError("Skill name 必须使用小写字母、数字和单连字符")
                for ref in re.findall(r"\]\(([^)]+)\)", body):
                    ref = ref.split("#", 1)[0]
                    if not ref or ref.startswith(("http://", "https://", "mailto:")):
                        continue
                    if ref not in item["references"]:
                        errors.append("引用文件不存在或路径越界：" + ref)
                if re.search(r"(?:scripts/|pip install|npm install|uv run|python[3]?\s+\S+\.py)", content):
                    errors.append("首版不执行包内脚本或安装程序依赖，请改用已注册工具")
                for dep in item["dependencies"]:
                    if not isinstance(dep, dict):
                        errors.append("依赖必须包含 server_id 与 tool_name")
                        continue
                    provider = dep.get("provider") or dep.get("provider_id")
                    if provider:
                        tool_name = dep.get("tool_name") or dep.get("tool_id")
                        problem = None
                        if provider != "ongrid" or tool_name not in {None, "alert_detail"}:
                            problem = "平台 API 依赖尚未注册：" + str(provider) + "/" + str(tool_name)
                        else:
                            cfg = (self.store.get("settings", "main") or {}).get("ongrid", {})
                            if not (cfg.get("url") or cfg.get("base_url")):
                                problem = "Ongrid API 尚未配置"
                            for reference in ("secret_id", "password_secret_id"):
                                if cfg.get(reference):
                                    try:
                                        self.vault.get(cfg[reference])
                                    except ConfigError as exc:
                                        problem = "Ongrid API 凭据不可用：" + str(exc)
                        if problem:
                            (errors if dep.get("required", True) and not dep.get("optional", False) else warnings).append(problem)
                        continue
                    server = self.store.get("mcp", dep.get("server_id", ""))
                    tool_name = dep.get("tool_name", dep.get("original_tool_name"))
                    problem = None
                    if not server or not server.get("current_version") or not server.get("enabled") or server.get("blocked"):
                        problem = "依赖 MCP 未发布、停用或被阻止：" + str(dep.get("server_id"))
                    else:
                        snapshot = self.published(server)
                        tool = next((t for t in snapshot.get("tools", []) if t.get("name") == tool_name), None)
                        if tool_name not in snapshot.get("allowed_tools", []) or not tool or tool.get("readonly") is not True:
                            problem = "依赖工具未明确开放为只读：" + str(tool_name)
                    if problem:
                        (errors if dep.get("required", True) and not dep.get("optional", False) else warnings).append(problem)
            elif kind == "mcp":
                if item["auth_type"] != "none" and not item.get("secret_id"):
                    errors.append("认证方式需要有效凭据")
                if not item.get("discovery", {}).get("complete"):
                    errors.append("请先完整发现工具目录后发布")
                tools = {t.get("name"): t for t in item["tools"]}
                aliases = set()
                for name in item["allowed_tools"]:
                    tool = tools.get(name)
                    if not tool or tool.get("readonly") is not True:
                        errors.append("白名单工具必须显式确认为只读：" + str(name))
                    elif not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.get("alias", "")) or tool["alias"] in aliases:
                        errors.append("工具别名无效或重复：" + str(name))
                    if tool:
                        aliases.add(tool.get("alias"))
            elif not item.get("secret_id"):
                warnings.append("尚未配置模型凭据；仅适用于无需认证的本地服务")
        except (ConfigError, yaml.YAMLError) as exc:
            errors.append(str(exc) if isinstance(exc, ConfigError) else "YAML frontmatter 无效")
        return {"valid": not errors, "errors": errors, "warnings": warnings, "checked_revision": item.get("revision"), "checked_at": utcnow(), "scope": "static_configuration"}

    @staticmethod
    def published(item, version_id=None):
        version_id = version_id or item.get("current_version")
        version = next((v for v in item.get("versions", []) if v["id"] == version_id), None)
        if not version:
            raise ConfigError("指定发布版本不存在")
        return copy.deepcopy(version["snapshot"])

    def publish(self, kind, id, expected_revision=None):
        def action():
            item = self.require(kind, id)
            if expected_revision is not None and item["revision"] != expected_revision:
                raise StoreConflict("发布时草稿已变化，请重新检查")
            validation = self.validate(kind, item)
            if not validation["valid"]:
                raise ConfigError("发布校验未通过：" + "；".join(validation["errors"]))
            snapshot = {k: copy.deepcopy(v) for k, v in item.items() if k not in {"versions", "validation", "current_version"}}
            snapshot.update(enabled=True, blocked=False, status="published")
            snapshot["artifact_digest"] = digest(snapshot)
            if kind == "skill":
                directory = self.artifact_dir / snapshot["artifact_digest"]
                directory.mkdir(exist_ok=True)
                files = {"SKILL.md": item["content"], **item["references"]}
                for name, content in files.items():
                    target = directory / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        if target.read_text() != content:
                            raise ConfigError("不可变 Skill 制品内容冲突")
                    else:
                        with target.open("x") as stream:
                            stream.write(content)
                        target.chmod(0o444)
                snapshot["artifact_path"] = str(directory)
            vid = "v_" + secrets.token_hex(12)
            version = {"id": vid, "created_at": utcnow(), "source_revision": item["revision"], "digest": digest(snapshot), "snapshot": snapshot}
            item["versions"].append(version)
            item.update(current_version=vid, enabled=True, blocked=False, status="published", validation=validation)
            saved = self.store.save(kind, id, item, item["revision"])
            self.audit("publish", kind, id, {"version_id": vid, "digest": version["digest"]})
            return saved
        return self.store.atomic(action)

    def rollback(self, kind, id, version_id):
        def action():
            item = self.require(kind, id)
            snapshot = self.published(item, version_id)
            validation = self.validate(kind, snapshot)
            if not validation["valid"]:
                raise ConfigError("回退版本已不可用：" + "；".join(validation["errors"]))
            item.update(current_version=version_id, status="published")
            saved = self.store.save(kind, id, item, item["revision"])
            self.audit("rollback", kind, id, {"version_id": version_id})
            return saved
        return self.store.atomic(action)

    def state(self, kind, id, payload):
        def action():
            item = self.require(kind, id)
            if not isinstance(payload.get("enabled"), bool):
                raise ConfigError("enabled 必须为布尔值")
            if payload["enabled"] and not item.get("current_version"):
                raise ConfigError("请先发布配置再启用")
            if "blocked" in payload and not isinstance(payload["blocked"], bool):
                raise ConfigError("blocked 必须为布尔值")
            item.update(enabled=payload["enabled"], blocked=payload.get("blocked", item.get("blocked", False)))
            saved = self.store.save(kind, id, item, payload.get("expected_revision", item["revision"]))
            self.audit("state", kind, id, {"enabled": item["enabled"], "blocked": item["blocked"]})
            return saved
        return self.store.atomic(action)

    def settings(self, payload):
        def action():
            previous = self.require("settings", "main")
            if payload.get("expected_revision") != previous["revision"]:
                raise StoreConflict("设置已变化，请重新读取后保存")
            item = copy.deepcopy(previous)
            for field in {"default_model_id", "budget", "queue_limit", "retention_days", "evidence_limit_bytes", "storage_quota_bytes", "ongrid", "environments", "identity_mapping", "query_catalog"}:
                if field in payload:
                    item[field] = copy.deepcopy(payload[field])
            if item.get("default_model_id"):
                model = self.require("model", item["default_model_id"])
                if not model.get("current_version") or not model.get("enabled") or model.get("blocked"):
                    raise ConfigError("默认模型必须已发布且启用")
            budget = item["budget"]
            for key, maximum in {"tool_calls": 100, "source_requests": 100, "model_calls": 30, "wall_seconds": 1800, "max_tokens": 131072, "evidence_bytes": 10485760}.items():
                if not isinstance(budget.get(key), int) or not 1 <= budget[key] <= maximum:
                    raise ConfigError(f"预算 {key} 必须为 1 至 {maximum} 的整数")
            for key, maximum in {"queue_limit": 10, "retention_days": 365, "evidence_limit_bytes": 10485760, "storage_quota_bytes": 1099511627776}.items():
                if not isinstance(item[key], int) or not 1 <= item[key] <= maximum:
                    raise ConfigError(f"{key} 必须为 1 至 {maximum} 的整数")
            ongrid = item.get("ongrid", {})
            if not isinstance(ongrid, dict):
                raise ConfigError("Ongrid 配置必须为对象")
            if ongrid.get("url") or ongrid.get("base_url"):
                validate_url(ongrid.get("url") or ongrid["base_url"], "Ongrid 地址")
            for input_field, reference_field in (("secret_value", "secret_id"), ("password_secret_value", "password_secret_id")):
                if input_field in ongrid:
                    value = ongrid.pop(input_field)
                    if value:
                        ongrid[reference_field] = self.vault.put(value)
                if ongrid.get(reference_field):
                    self.vault.get(ongrid[reference_field])
            self._reject_embedded_secrets(item)
            saved = self.store.save("settings", "main", item, previous["revision"])
            self.audit("save", "settings", "main", {"revision": saved["revision"]})
            return saved
        return self.store.atomic(action)

    def _reject_embedded_secrets(self, item):
        if isinstance(item, dict):
            for key, value in item.items():
                if str(key).lower() in _SENSITIVE_KEYS or str(key).lower().endswith("_secret_value"):
                    raise ConfigError("凭据必须通过 secret_value 写入并保存为 secret_id")
                self._reject_embedded_secrets(value)
        elif isinstance(item, list):
            for value in item:
                self._reject_embedded_secrets(value)
