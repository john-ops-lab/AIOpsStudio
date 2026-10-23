"""Fail-closed, network-free source authorization and typed model disclosure.

Only server-side callers supply identity, frozen configuration and typed material.
Snapshots bound the maximum permission of an attempt; the current policy is read
again on *every* check. Neither an old snapshot nor an administrator role grants
access. Alias records are private Store documents, never a model/client payload.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import re
import secrets
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class PolicyError(ValueError):
    """Fixed public messages deliberately omit targets, URLs and source errors."""

    def __init__(self, code: str, detail: str = "当前授权或数据出站策略不允许此操作"):
        self.code, self.detail = code, detail
        super().__init__(detail)


_DIMENSIONS = {"subjects": "subject_id", "sources": "source_id", "tools": "tool_name",
               "targets": "target", "environments": "environment", "purposes": "purpose"}
_CREDENTIAL_FIELDS = {"password", "secret", "secret_value", "api_key", "apikey", "access_token",
    "refresh_token", "authorization", "cookie", "set-cookie", "ciphertext", "master_key",
    "client_secret", "password_hash", "session_hash", "x-api-key", "x-auth-token",
    "secret_id", "password_secret_id", "credential", "credentials", "egress_alias", "alias_mapping"}
_ACTIONS = {"allow", "text", "alias", "drop", "deny"}
_PUBLIC_CODES = {"missing": "策略尚未配置，操作未派发", "invalid": "策略无效，操作未派发",
    "unavailable": "无法读取当前策略，操作未派发", "source": "当前主体没有此源范围权限",
    "material": "材料不存在或当前主体没有读取权限", "egress": "材料不符合当前模型数据出站规则"}


def _error(kind: str, suffix: str = ""):
    raise PolicyError("policy_" + kind + ("_" + suffix if suffix else ""), _PUBLIC_CODES.get(kind, _PUBLIC_CODES["invalid"]))


def _strings(value, *, nonempty=True):
    return isinstance(value, list) and (bool(value) or not nonempty) and all(isinstance(v, str) and v != "" for v in value)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        _error("invalid", "json")


def _pointer(path: str):
    if not isinstance(path, str) or (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
        _error("invalid", "path")
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]] if path else []


def _path(parts):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _rule_for(fields, parts):
    matches = []
    for path, rule in fields.items():
        expected = _pointer(path)
        # '*' is an array index only. It cannot authorize arbitrary object keys.
        if len(expected) == len(parts) and all(a == str(b) or (a == "*" and isinstance(b, int)) for a, b in zip(expected, parts)):
            matches.append((sum(part != "*" for part in expected), rule))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0] and matches[0][1] != matches[1][1]:
        _error("invalid", "ambiguous_path")
    return matches[0][1]


def _leaves(value, parts=()):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            if not isinstance(key, str):
                _error("invalid", "field")
            yield from _leaves(item, (*parts, key))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _leaves(item, (*parts, index))
    else:
        yield parts, value


def _contains_credential(value):
    if isinstance(value, dict):
        return any(str(key).lower().replace("-", "_") in {v.replace("-", "_") for v in _CREDENTIAL_FIELDS}
                   or str(key).lower().endswith("_secret_value") or _contains_credential(item) for key, item in value.items())
    return isinstance(value, list) and any(_contains_credential(item) for item in value)


def _destination(config):
    if not isinstance(config, dict):
        _error("invalid", "connection")
    if config.get("verify_tls") is False or config.get("tls_verify") is False or config.get("follow_redirects"):
        _error("source", "connection")
    value = config.get("url") or config.get("base_url")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        port = parsed.port
        host = parsed.hostname.lower()
        authority = "[" + host + "]" if ":" in host else host
        if port and port != (443 if parsed.scheme == "https" else 80):
            authority += ":" + str(port)
        url = urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))
    except (ValueError, TypeError, AttributeError):
        _error("invalid", "destination")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    headers = config.get("headers") or config.get("default_headers") or {}
    if not isinstance(headers, dict):
        _error("invalid", "headers")
    hosts = [str(v) for k, v in headers.items() if str(k).lower() == "host"]
    if len(hosts) > 1:
        _error("invalid", "host")
    refs = [config[k] for k in ("secret_id", "password_secret_id") if config.get(k)]
    if any(not isinstance(ref, str) for ref in refs):
        _error("invalid", "credentials")
    return {"url": url, "scheme": parsed.scheme, "loopback": loopback, "host_header": hosts[0] if hosts else None,
            "secret_ids": sorted(set(refs))}


def _connection_matches(rule, config):
    actual = _destination(config)
    refs = rule.get("secret_ids")
    if not _strings(refs, nonempty=False) or sorted(set(refs)) != actual["secret_ids"]:
        return False
    destinations = rule.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        _error("invalid", "destination_rule")
    for destination in destinations:
        if not isinstance(destination, dict) or not isinstance(destination.get("url"), str):
            _error("invalid", "destination_rule")
        approved = _destination({"url": destination["url"]})
        if approved["url"] == actual["url"] and destination.get("host_header") == actual["host_header"]:
            if actual["scheme"] == "https" or (actual["loopback"] and destination.get("allow_http_loopback") is True):
                return True
    return False


def _match_values(values, value):
    if not _strings(values):
        _error("invalid", "selectors")
    return "*" in values or ("" if value is None else value) in values


def _matches(rule, request, *, deny=False):
    if not isinstance(rule, dict):
        _error("invalid", "rule")
    return all(_match_values(rule.get(key, ["*"] if deny else None), request[field]) for key, field in _DIMENSIONS.items())


def _argument_scope(rule, request):
    scope = rule.get("argument_scope")
    if not isinstance(scope, dict):
        _error("invalid", "argument_scope")
    if scope.get("mode") == "source":
        # A tool that can query arbitrary objects needs explicit full-source rights.
        return rule.get("targets") == ["*"]
    fields = scope.get("fields")
    if scope.get("mode") != "exact" or not isinstance(fields, dict) or not fields:
        _error("invalid", "argument_scope")
    leaves = list(_leaves(request["arguments"]))
    for parts, value in leaves:
        expected = _rule_for(fields, parts)
        if expected is None:
            return False
        if expected in ("$target", "$environment"):
            if value != request[expected[1:]]:
                return False
        elif isinstance(expected, dict) and set(expected) == {"equals"}:
            if value != expected["equals"]:
                return False
        elif isinstance(expected, dict) and set(expected) == {"enum"} and isinstance(expected["enum"], list):
            if value not in expected["enum"]:
                return False
        else:
            _error("invalid", "argument_constraint")
    # No required bound field may be omitted (wildcard array rules need at least one leaf).
    return all(any(_rule_for({path: expected}, parts) is not None for parts, _ in leaves) for path, expected in fields.items())


class PolicyService:
    """Store-backed policy boundary. All return scopes remain server-only."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def validate_document(kind, doc):
        """Validate an admin draft without granting its author data access.

        Disabled documents are valid to save, and deny all runtime checks.
        Version is a caller-chosen immutable identifier; Store revision/CAS is
        managed by the API, not by this pure policy module.
        """
        schemas = {"access_policy": "source-access/v1", "egress_policy": "model-egress/v1"}
        if kind not in schemas or not isinstance(doc, dict) or not isinstance(doc.get("enabled"), bool):
            _error("invalid", "document")
        candidate = copy.deepcopy(doc)
        candidate["enabled"] = True
        PolicyService._valid(candidate, schemas[kind])
        if kind == "access_policy":
            if not isinstance(doc.get("grants"), list) or not isinstance(doc.get("denies"), list):
                _error("invalid", "rules")
            for deny in doc["denies"]:
                if not isinstance(deny, dict) or set(deny) - set(_DIMENSIONS):
                    _error("invalid", "deny")
                for values in deny.values():
                    if not _strings(values):
                        _error("invalid", "selectors")
            for grant in doc["grants"]:
                if not isinstance(grant, dict) or any(not _strings(grant.get(key)) for key in _DIMENSIONS):
                    _error("invalid", "selectors")
                PolicyService._validate_binding(grant)
                scope = grant.get("argument_scope")
                if not isinstance(scope, dict) or scope.get("mode") not in {"source", "exact"}:
                    _error("invalid", "argument_scope")
                if scope["mode"] == "source":
                    if grant["targets"] != ["*"]:
                        _error("invalid", "argument_scope")
                else:
                    fields = scope.get("fields")
                    if not isinstance(fields, dict) or not fields:
                        _error("invalid", "argument_scope")
                    for path, constraint in fields.items():
                        _pointer(path)
                        if constraint in ("$target", "$environment"):
                            continue
                        if not isinstance(constraint, dict) or not (set(constraint) == {"equals"} or
                                set(constraint) == {"enum"} and isinstance(constraint["enum"], list) and constraint["enum"]):
                            _error("invalid", "argument_constraint")
        else:
            if not isinstance(doc.get("model_bindings"), list) or not isinstance(doc.get("types"), dict) or not _strings(doc.get("denied_types"), nonempty=False):
                _error("invalid", "types")
            for binding in doc["model_bindings"]:
                if not isinstance(binding, dict) or not _strings(binding.get("subjects")) or not _strings(binding.get("models")):
                    _error("invalid", "model_bindings")
                PolicyService._validate_binding(binding)
            for name, rule in doc["types"].items():
                if not isinstance(name, str) or not name or not isinstance(rule, dict) or not isinstance(rule.get("fields"), dict):
                    _error("invalid", "fields")
                limit = rule.get("max_bytes")
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 8 * 1048576:
                    _error("invalid", "limit")
                for path, action in rule["fields"].items():
                    _pointer(path)
                    if not isinstance(action, str) or action not in _ACTIONS:
                        _error("invalid", "action")
        return copy.deepcopy(doc)

    @staticmethod
    def _validate_binding(rule):
        if not _strings(rule.get("secret_ids"), nonempty=False) or not isinstance(rule.get("destinations"), list) or not rule["destinations"]:
            _error("invalid", "binding")
        for destination in rule["destinations"]:
            if not isinstance(destination, dict) or not isinstance(destination.get("url"), str):
                _error("invalid", "destination_rule")
            actual = _destination({"url": destination["url"]})
            if actual["scheme"] == "http" and not (actual["loopback"] and destination.get("allow_http_loopback") is True):
                _error("invalid", "insecure_destination")
            if "host_header" in destination and (not isinstance(destination["host_header"], str) or not destination["host_header"] or "\r" in destination["host_header"] or "\n" in destination["host_header"]):
                _error("invalid", "host")

    def _read(self, kind, schema, *, missing=False):
        try:
            doc = self.store.get(kind, "main")
        except Exception:
            _error("unavailable")
        if not doc:
            if missing:
                return None
            _error("missing")
        self.validate_document(kind, doc)
        self._valid(doc, schema)
        return copy.deepcopy(doc)

    @staticmethod
    def _valid(doc, schema):
        if not isinstance(doc, dict) or doc.get("schema") != schema or not isinstance(doc.get("version"), str) or not doc["version"]:
            _error("invalid", "version")
        if doc.get("enabled") is not True:
            _error("source" if schema == "source-access/v1" else "egress", "disabled")
        _json(doc)

    def snapshot(self):
        """Missing policies can be recorded; they never authorize a dispatch."""
        return {"schema": "policy-snapshot/v1", "source": self._read("access_policy", "source-access/v1", missing=True),
                "egress": self._read("egress_policy", "model-egress/v1", missing=True)}

    def _policies(self, kind, schema, snapshot, key):
        latest = self._read(kind, schema)
        if snapshot is None:
            return [latest]
        if not isinstance(snapshot, dict) or snapshot.get("schema") != "policy-snapshot/v1" or not snapshot.get(key):
            _error("missing", "snapshot")
        frozen = snapshot[key]
        self.validate_document(kind, frozen)
        self._valid(frozen, schema)
        return [latest, frozen]

    def authorize_source(self, *, subject_id, source_id, tool_name, config, arguments, target=None,
                         environment=None, purpose="investigation", snapshot=None):
        if (not all(isinstance(value, str) and value for value in (subject_id, source_id, tool_name, purpose))
                or not isinstance(arguments, dict) or any(value is not None and not isinstance(value, str) for value in (target, environment))):
            _error("invalid", "request")
        _json(arguments)
        request = dict(subject_id=subject_id, source_id=source_id, tool_name=tool_name, target=target,
                       environment=environment, purpose=purpose, arguments=copy.deepcopy(arguments))
        policies = self._policies("access_policy", "source-access/v1", snapshot, "source")
        for policy in policies:
            if not isinstance(policy.get("grants"), list) or not isinstance(policy.get("denies"), list):
                _error("invalid", "rules")
            if any(_matches(rule, request, deny=True) for rule in policy["denies"]):
                _error("source", "denied")
            if not any(_matches(rule, request) and _connection_matches(rule, config) and _argument_scope(rule, request) for rule in policy["grants"]):
                _error("source", "scope")
        connection = _destination(config)
        return {**request, "config": {"url": connection["url"], "secret_id": config.get("secret_id"),
                "password_secret_id": config.get("password_secret_id"), "verify_tls": True,
                "headers": {"Host": connection["host_header"]} if connection["host_header"] else {}},
                "policy_versions": [policy["version"] for policy in policies]}

    def authorize_material(self, record, *, subject_id, snapshot=None):
        """Private history plus current source scope; legacy unknown origin refuses."""
        if not isinstance(record, dict) or record.get("owner_id") != subject_id:
            _error("material")
        scopes = record.get("access_scopes")
        if not isinstance(scopes, list) or not scopes:
            _error("material", "origin_unknown")
        for scope in scopes:
            if not isinstance(scope, dict):
                _error("material", "origin_unknown")
            try:
                self.authorize_source(**{key: scope[key] for key in ("source_id", "tool_name", "config", "arguments", "target", "environment", "purpose")},
                                      subject_id=subject_id, snapshot=snapshot)
            except KeyError:
                _error("material", "origin_unknown")
        return True

    def authorize_model(self, model_config, *, subject_id, snapshot=None):
        policies = self._policies("egress_policy", "model-egress/v1", snapshot, "egress")
        for policy in policies:
            bindings = policy.get("model_bindings")
            if not isinstance(bindings, list):
                _error("invalid", "model_bindings")
            if not any(isinstance(rule, dict) and _match_values(rule.get("subjects"), subject_id)
                       and _match_values(rule.get("models"), model_config.get("id"))
                       and _connection_matches(rule, model_config) for rule in bindings):
                _error("egress", "model_binding")
        return [policy["version"] for policy in policies]

    def _alias(self, subject_id, scope_id, value):
        if not isinstance(value, str) or not value:
            _error("egress", "identifier")
        identity = hashlib.sha256((subject_id + "\0" + scope_id).encode()).hexdigest()
        def save():
            record = self.store.get("egress_alias", identity) or {"owner_id": subject_id, "scope_id": scope_id, "entries": []}
            if record.get("owner_id") != subject_id or record.get("scope_id") != scope_id:
                _error("egress", "alias_scope")
            entries = record.get("entries")
            if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("value"), str)
                    or not re.fullmatch(r"resource_[0-9a-f]{24}", item.get("alias", "")) for item in entries):
                _error("invalid", "alias_record")
            for item in entries:
                if item.get("value") == value:
                    return item["alias"]
            if len(entries) >= 10000:
                _error("egress", "alias_limit")
            alias = "resource_" + secrets.token_hex(12)
            while any(item["alias"] == alias for item in entries):
                alias = "resource_" + secrets.token_hex(12)
            entries.append({"value": value, "alias": alias})
            self.store.save("egress_alias", identity, record)
            return alias
        try:
            return self.store.atomic(save)
        except PolicyError:
            raise
        except Exception:
            _error("unavailable", "alias")

    def _alias_entries(self, subject_id, scope_id):
        identity = hashlib.sha256((subject_id + "\0" + scope_id).encode()).hexdigest()
        try:
            record = self.store.get("egress_alias", identity)
        except Exception:
            _error("unavailable", "alias")
        if not record:
            return []
        if record.get("owner_id") != subject_id or record.get("scope_id") != scope_id or not isinstance(record.get("entries"), list):
            _error("egress", "alias_scope")
        entries = record["entries"]
        if any(not isinstance(entry, dict) or not isinstance(entry.get("value"), str)
               or not re.fullmatch(r"resource_[0-9a-f]{24}", entry.get("alias", "")) for entry in entries):
            _error("invalid", "alias_record")
        if len({entry["alias"] for entry in entries}) != len(entries) or len({entry["value"] for entry in entries}) != len(entries):
            _error("invalid", "alias_record")
        return copy.deepcopy(entries)

    def resolve_aliases(self, value, *, subject_id, scope_id, text=False):
        """Resolve only this scope's tokens, before source authorization.

        Default mode accepts an alias only as a whole JSON string value; it
        never rewrites a query expression. ``text=True`` is only for authorized
        server-side report rendering. Unknown/embedded tokens never guess.
        """
        entries = self._alias_entries(subject_id, scope_id)
        mapping = {entry["alias"]: entry["value"] for entry in entries}
        token = re.compile(r"(?<![\w])resource_[0-9a-f]{24}(?![\w])")
        def walk(item):
            if isinstance(item, dict):
                return {key: walk(child) for key, child in item.items()}
            if isinstance(item, list):
                return [walk(child) for child in item]
            if not isinstance(item, str):
                return copy.deepcopy(item)
            found = token.findall(item)
            if any(alias not in mapping for alias in found):
                _error("egress", "alias_unknown")
            if text:
                return token.sub(lambda match: mapping[match.group()], item)
            if item in mapping:
                return mapping[item]
            if found:
                _error("egress", "alias_embedded")
            return item
        return walk(value)

    def _alias_text(self, value, *, subject_id, scope_id):
        # This runs only on a typed model text view, never source arguments.
        # Whole token boundaries prevent svc-1 from corrupting svc-10.
        entries = self._alias_entries(subject_id, scope_id)
        mapping = {entry["value"]: entry["alias"] for entry in entries}
        if not mapping:
            return value
        pattern = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(original) for original in sorted(mapping, key=len, reverse=True)) + r")(?![\w])")
        return pattern.sub(lambda match: mapping[match.group()], value)

    @staticmethod
    def _check_text(value, credentials):
        if not isinstance(value, str):
            _error("egress", "text")
        if any(secret and secret in value for secret in credentials) or re.search(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", value):
            _error("egress", "credential")
        return value

    def egress_view(self, kind, value, *, subject_id, scope_id, model_config, snapshot, credentials=()):
        """Project typed material only. This never rewrites a source request.

        ``allow`` accepts JSON scalars, ``text`` accepts explicit business text,
        ``alias`` accepts a structured identifier, and ``drop`` omits a field.
        Every leaf must be registered by both frozen and current policies.
        """
        if not isinstance(kind, str) or not isinstance(subject_id, str) or not subject_id or not isinstance(scope_id, str) or not scope_id:
            _error("invalid", "egress_scope")
        policies = self._policies("egress_policy", "model-egress/v1", snapshot, "egress")
        self.authorize_model(model_config, subject_id=subject_id, snapshot=snapshot)
        if not isinstance(credentials, (list, tuple)) or any(not isinstance(secret, str) for secret in credentials):
            _error("invalid", "credentials")
        if _contains_credential(value):
            _error("egress", "credential_field")
        type_rules = []
        for policy in policies:
            types, denied = policy.get("types"), policy.get("denied_types")
            if not isinstance(types, dict) or not _strings(denied, nonempty=False):
                _error("invalid", "types")
            if kind in denied or kind not in types:
                _error("egress", "type")
            rule = types[kind]
            if not isinstance(rule, dict) or not isinstance(rule.get("fields"), dict):
                _error("invalid", "fields")
            limit = rule.get("max_bytes")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 8 * 1048576:
                _error("invalid", "limit")
            if any(not isinstance(action, str) or action not in _ACTIONS for action in rule["fields"].values()):
                _error("invalid", "action")
            if len(_json(value).encode()) > limit:
                _error("egress", "limit")
            type_rules.append(rule)
        _DROP = object()
        # Register all declared identifiers before processing adjacent text so
        # dict order cannot break identity linkage within a single payload.
        for parts, item in _leaves(value):
            actions = [_rule_for(rule["fields"], parts) for rule in type_rules]
            if "alias" in actions and not any(action in {None, "deny", "drop"} for action in actions):
                self._check_text(item, credentials)
                self._alias(subject_id, scope_id, item)
        def walk(item, parts=()):
            if isinstance(item, dict) and item:
                result = {}
                for key, child in item.items():
                    transformed = walk(child, (*parts, key))
                    if transformed is not _DROP:
                        result[key] = transformed
                return result
            if isinstance(item, list) and item:
                result = [walk(child, (*parts, index)) for index, child in enumerate(item)]
                return [child for child in result if child is not _DROP]
            actions = [_rule_for(rule["fields"], parts) for rule in type_rules]
            if any(action is None or action == "deny" for action in actions):
                _error("egress", "field")
            if "drop" in actions:
                return _DROP
            if "alias" in actions:
                if any(action not in {"alias", "allow", "text"} for action in actions):
                    _error("egress", "action")
                self._check_text(item, credentials)
                return self._alias(subject_id, scope_id, item)
            if "text" in actions:
                return self._alias_text(self._check_text(item, credentials), subject_id=subject_id, scope_id=scope_id)
            if isinstance(item, (dict, list)):
                # Empty containers require their own explicit leaf rule.
                return copy.deepcopy(item)
            if isinstance(item, str):
                return self._alias_text(self._check_text(item, credentials), subject_id=subject_id, scope_id=scope_id)
            if item is not None and (not isinstance(item, (int, float, bool)) or isinstance(item, float) and not math.isfinite(item)):
                _error("egress", "scalar")
            return item
        result = walk(value)
        if result is _DROP:
            _error("egress", "empty")
        if any(len(_json(result).encode()) > rule["max_bytes"] for rule in type_rules):
            _error("egress", "limit")
        return result


def policy_digest(snapshot):
    """Audit digest is safe to persist; snapshots themselves stay server-only."""
    return hashlib.sha256(_json(snapshot).encode()).hexdigest()
