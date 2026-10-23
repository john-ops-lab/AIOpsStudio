"""Deterministic context, latest policy checks and typed model-wire boundary."""
from __future__ import annotations

import copy
from contextvars import ContextVar
import hashlib
import json
from urllib.parse import urlsplit

from fastapi import HTTPException

from .access_policy import PolicyError, PolicyService, policy_digest
from .analysis_context import ContextError, bind_query, coverage_record, freeze_context, legacy_context
from .material_reading import MaterialReader, ReadingError, canonical, logical_document

ACTIVE_MODEL = ContextVar("aiopsstudio_model_dispatch", default=None)


class TrustBoundary:
    @property
    def policies(self):
        return PolicyService(self.store)

    def _scope_id(self, run):
        return run.get("conversation_id") or run["thread_id"]

    def _credentials(self):
        # Local revocation stops use, but does not prove remote rotation. Old
        # credential values therefore remain forbidden material.
        return self.vault.sensitive_values()

    def _reader(self):
        return MaterialReader(self.store, self.artifact_dir, authorize_material=lambda record, owner, run:
            self.policies.authorize_material(record, subject_id=owner, snapshot=run.get("policy_snapshot")))

    def authorize_history(self, run, owner, _seen=None, *, allow_legacy_archive=False):
        if (run.get("owner_id") or "admin") != owner:
            raise HTTPException(404, "分析不存在或当前不可访问")
        from .legacy_archive import archived, deny_execution
        if allow_legacy_archive and archived(self.store, "run", run, owner):
            return run
        deny_execution(self.store, "run", run)
        seen = set(_seen or ())
        if run.get("id") in seen:
            raise HTTPException(404, "分析不存在或当前不可访问")
        seen.add(run.get("id"))
        report = run.get("report") or {}
        derived = (not report.get("expired") and (report.get("analysis") or report.get("summary"))) or any(m.get("role") == "assistant" for m in run.get("messages", []))
        if derived and not run.get("evidence") and run.get("trust_version") != "trusted-runtime/1":
            raise HTTPException(404, "分析不存在或当前不可访问")
        try:
            # A revoked in-flight response may correctly leave no evidence.
            # Its saved invocation arguments still carry source-bound identity.
            for invocation in self.store.list("invocation"):
                if invocation.get("run_id") == run.get("id") and invocation.get("source") not in {None, "model"}:
                    self.policies.authorize_material(invocation, subject_id=owner, snapshot=run.get("policy_snapshot"))
            for evidence in run.get("evidence", []):
                self.policies.authorize_material(evidence, subject_id=owner, snapshot=run.get("policy_snapshot"))
            for previous in run.get("history_context", {}).get("rounds", []):
                prior = self.store.get("run", previous.get("run_id", ""))
                if not prior:
                    raise PolicyError("policy_material_history_missing")
                self.authorize_history(prior, owner, seen)
        except PolicyError:
            raise HTTPException(404, "分析不存在或当前不可访问") from None
        return run

    def _source_config(self, run, source):
        if source == "ongrid":
            from .connectors import _ongrid
            return _ongrid(run["config_snapshot"]["settings"])
        config = next((s for s in run["config_snapshot"].get("mcps", []) if s["id"] == source), None)
        if not config:
            raise PolicyError("policy_source_missing")
        return config

    def _source_scope(self, run, source, tool, arguments, *, purpose=None):
        self._guard(run["id"])
        current = self.store.get("mcp", source) if source != "ongrid" else None
        if current and current.get("blocked"):
            raise PolicyError("policy_source_blocked")
        config = self._source_config(run, source)
        for key, reference in config.items():
            if key.endswith("secret_id") and reference:
                self.vault.get(reference)
        return self.policies.authorize_source(subject_id=run.get("owner_id") or "admin", source_id=source,
            tool_name=tool, config=config, arguments=arguments,
            target=None if purpose == "discovery" else run.get("target"), environment=run.get("environment"), purpose=purpose or run["purpose"],
            snapshot=run.get("policy_snapshot"))

    def _bind_source(self, run, source, name, arguments, definition=None):
        arguments = self.policies.resolve_aliases(arguments, subject_id=run.get("owner_id") or "admin", scope_id=self._scope_id(run))
        if run["purpose"] not in {"investigation", "skill_test"}:
            return arguments, None
        bound = bind_query(run.get("analysis_context") or legacy_context(), source, name, arguments,
                           definition_digest=(definition or {}).get("definition_digest"))
        entry = next(e for e in run["analysis_context"]["query_catalog"]["entries"]
                     if e["source_id"] == source and e["tool_name"] == name)
        for key in ("unit", "assessment_rule", "source_group", "source_lineage"):
            if key in entry:
                bound["binding"][key] = copy.deepcopy(entry[key])
        return bound["arguments"], bound["binding"]

    def _source_http_dispatch(self, run_id, source, tool, arguments, *, purpose=None):
        self._source_scope(self.store.get("run", run_id), source, tool, arguments, purpose=purpose)
        self._count(run_id, source_requests=1)
        if source != "ongrid":
            self._update(run_id, callback=lambda r: r["usage"].update(
                mcp_transport_http_request_count=r["usage"].get("mcp_transport_http_request_count", 0) + 1))

    def _identity_disclosable(self, run):
        """A queued owner does not acquire catalog identities by guessing aliases."""
        if (run.get("owner_id") or "admin") == "admin":
            return True  # Configuration administrators already inspect mappings.
        if run.get("evidence"):
            return True  # require_owned has just reauthorized its source scopes.
        from . import connectors
        context = run.get("analysis_context") or {}
        for entry in context.get("query_catalog", {}).get("entries", []):
            source, tool = entry["source_id"], entry["tool_name"]
            if entry.get("object_scope", "target") != "target":
                continue
            config = next((item for item in run.get("config_snapshot", {}).get("mcps", []) if item["id"] == source), None)
            if not config:
                continue
            try:
                definition = connectors.permitted_tool(config, tool)
                arguments = bind_query(context, source, tool, {}, definition_digest=connectors.tool_digest(definition))["arguments"]
                self.policies.authorize_source(subject_id=run["owner_id"], source_id=source, tool_name=tool,
                    config=config, arguments=arguments, target=run.get("target"), environment=run.get("environment"),
                    purpose=run["purpose"], snapshot=run.get("policy_snapshot"))
                return True
            except (PolicyError, ContextError, connectors.ConnectorError):
                continue
        return False

    def _typed(self, run, kind, value):
        return self.policies.egress_view(kind, value, subject_id=run.get("owner_id") or "admin",
            scope_id=self._scope_id(run), model_config=run["config_snapshot"]["model"],
            snapshot=run.get("policy_snapshot"), credentials=self._credentials())

    def _approved_text(self, run, text):
        if not hasattr(self, "_approved_outbound"):
            self._approved_outbound = {}
        approved = self._approved_outbound.setdefault(run["id"], {})
        approved[hashlib.sha256(text.encode()).hexdigest()] = policy_digest(self.policies.snapshot())
        return text

    def _typed_text(self, run, kind, text):
        return self._approved_text(run, self._typed(run, kind, {"text": text})["text"])

    def _investigation_context(self, run, *, report=False):
        self.authorize_history(run, run.get("owner_id") or "admin")
        raw = self._raw_investigation_context(run, report=report)
        # Global mappings/catalogs can contain other teams. Only the selected
        # identity and version metadata enter a model view.
        raw.pop("identity_mapping", None)
        raw.pop("query_catalog", None)
        messages, history, attachments = (raw.pop(k, None) for k in ("received_user_messages", "history_context", "user_materials"))
        ledger = raw.pop("evidence_ledger")
        raw["identity"] = (run.get("analysis_context") or {}).get("identity", {}).get("canonical")
        raw["windows"] = (run.get("analysis_context") or {}).get("windows", {})
        raw["reference_manifest"] = [{"skill_id": skill["id"], "version_id": skill["version_id"],
            "path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
            for skill in (run["config_snapshot"].get("skill"), run["config_snapshot"].get("cmdb_skill")) if skill
            for path, text in skill.get("references", {}).items()]
        result = self._typed(run, "context", raw)
        result["received_user_messages"] = [self._typed(run, "user_message", m) for m in messages or []]
        result["history_context"] = self._typed(run, "history", history) if history else {}
        result["user_materials"] = [self._typed(run, "attachment", a) for a in attachments or []]
        result["evidence_ledger"] = self._typed(run, "source_evidence", ledger)
        # A tool result must survive the next bounded-context rebuild. It is
        # projected again under the latest policy; old approval is insufficient.
        result["material_reads"] = [self._typed(run, "reading", receipt["result"])
            for receipt in self.store.list("material_read")
            if receipt.get("run_id") == run["id"] and receipt.get("consumer") == "model" and receipt.get("result")]
        result["material_reads"].extend(self._typed(run, "reading", error) for error in run.get("reading_errors", []))
        return result

    async def _model_http_guard(self, request):
        """Inspect the *actual* SDK HTTP request before any bytes are sent."""
        current = ACTIVE_MODEL.get()
        if not current or current[0] is not self:
            raise PolicyError("policy_egress_unbound_dispatch")
        run = self.store.get("run", current[1])
        self._guard(run["id"])
        self.authorize_history(run, run.get("owner_id") or "admin")
        model = run["config_snapshot"]["model"]
        self.policies.authorize_model(model, subject_id=run.get("owner_id") or "admin", snapshot=run.get("policy_snapshot"))
        base = (model.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        expected = urlsplit(base + "/chat/completions")
        actual = urlsplit(str(request.url))
        if (actual.scheme, actual.netloc, actual.path, actual.query) != (expected.scheme, expected.netloc, expected.path, ""):
            raise PolicyError("policy_egress_destination")
        key = self.vault.get(model["secret_id"])
        if request.headers.get("authorization") != "Bearer " + key or request.headers.get("host") != request.url.netloc.decode():
            raise PolicyError("policy_egress_auth_binding")
        for name, value in request.headers.items():
            if name != "authorization" and any(secret and secret in value for secret in self._credentials()):
                raise PolicyError("policy_egress_credential_header")
        try:
            body = json.loads(request.content)
            for secret in self._credentials():
                if secret and secret in canonical(body):
                    raise PolicyError("policy_egress_credential")
            # SDK framework instructions are also typed, never exempted merely
            # because they are a SystemMessage. Other text must be the result
            # of a typed server composition in this exact attempt.
            for message in body.get("messages", []):
                content = message.get("content")
                if message.get("role") in {"assistant", "tool"}:
                    normalized = copy.deepcopy(message)
                    if content is None and message.get("role") == "assistant" and message.get("tool_calls"):
                        normalized["content"] = ""
                    elif not isinstance(content, str):
                        raise PolicyError("policy_egress_content_type")
                    if self._typed(run, "model_protocol", normalized) != normalized:
                        raise PolicyError("policy_egress_unprojected_protocol")
                    continue
                if not isinstance(content, str):
                    raise PolicyError("policy_egress_content_type")
                if message.get("role") == "system":
                    projected = self._typed_text(run, "prompt", content)
                    if projected != content:
                        raise PolicyError("policy_egress_unprojected_system")
                elif getattr(self, "_approved_outbound", {}).get(run["id"], {}).get(hashlib.sha256(content.encode()).hexdigest()) != policy_digest(self.policies.snapshot()):
                    raise PolicyError("policy_egress_untyped_message")
            if body.get("tools"):
                approved = self._typed(run, "tool_definition", body["tools"])
                if approved != body["tools"]:
                    raise PolicyError("policy_egress_unprojected_tool")
            parameters = {key: value for key, value in body.items() if key not in {"messages", "tools"}}
            if self._typed(run, "model_parameters", parameters) != parameters:
                raise PolicyError("policy_egress_unprojected_parameters")
            # No credential-like field, including extra_body, is allowed in a
            # payload. The provider credential belongs solely in bound auth.
            from .access_policy import _contains_credential
            if _contains_credential(body):
                raise PolicyError("policy_egress_credential_field")
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(error, PolicyError):
                raise
            raise PolicyError("policy_egress_payload_invalid") from None
        self._update(run["id"], callback=lambda r: r["usage"].update(model_http_request_count=r["usage"].get("model_http_request_count", 0) + 1))
        self._event(run["id"], "model_egress_checked", {"policy_digest": policy_digest(run.get("policy_snapshot")), "typed": True})

    def _coverage(self, run, native, binding, *, truncated=False):
        doc, _ = logical_document(native)
        status, count, begin, end, pages, stale = "unknown", None, None, None, None, False
        if isinstance(doc, dict):
            supplied = doc.get("coverage") if isinstance(doc.get("coverage"), dict) else {}
            states = {"complete", "empty", "forbidden", "failed", "stale", "partial", "historical_unsupported", "unknown"}
            explicit_status = supplied.get("status") if supplied.get("status") in states else None
            failed = bool(isinstance(native, dict) and native.get("isError"))
            status = "failed" if failed else explicit_status or "unknown"
            begin, end = supplied.get("start"), supplied.get("end")
            pages = supplied.get("pagination_complete") if type(supplied.get("pagination_complete")) is bool else None
            stale = supplied.get("stale") is True
            truncated = truncated or supplied.get("truncated") is True or doc.get("truncated") is True
            continuation = any(value.get("has_more") is True or value.get("next_cursor") not in (None, "")
                               or value.get("continuation_token") not in (None, "") for value in (doc, supplied))
            if continuation:
                pages = False
            data = doc.get("data")
            if (doc.get("status") == "success" and isinstance(data, dict)
                    and data.get("resultType") in {"matrix", "vector"} and isinstance(data.get("result"), list)):
                rows = data["result"]; count = len(rows)
                try:
                    from datetime import datetime, timezone
                    import math
                    samples = []
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get("metric"), dict):
                            raise ValueError()
                        points = row.get("values") if data["resultType"] == "matrix" else [row.get("value")]
                        if not isinstance(points, list):
                            raise ValueError()
                        for point in points:
                            if (not isinstance(point, list) or len(point) != 2 or type(point[0]) not in (int, float)
                                    or not math.isfinite(point[0])):
                                raise ValueError()
                            samples.append(point[0])
                    if samples:
                        begin = datetime.fromtimestamp(min(samples), timezone.utc).isoformat()
                        end = datetime.fromtimestamp(max(samples), timezone.utc).isoformat()
                    # Success is a default only. Source limitations take precedence.
                    if not failed and explicit_status is None:
                        status = "empty" if not rows else "complete"
                    elif status == "empty" and rows:
                        status = "partial"
                    if pages is None:
                        pages = True
                except (ValueError, TypeError, OverflowError, OSError):
                    begin, end = None, None
                    if status not in {"failed", "forbidden"}:
                        status = "unknown"
            elif isinstance(doc.get("items"), list):
                count = len(doc["items"])
                if pages is None:
                    pages = True if doc.get("has_more") is False else None
                if supplied.get("status") in {"complete", "empty"} and pages is not True:
                    status = "partial"
        result = coverage_record(binding, status=status, item_count=count, observed_start=begin, observed_end=end,
            pagination_complete=pages, truncated=truncated, stale=stale, historical=(binding or {}).get("kind") == "topology")
        result["binding"] = copy.deepcopy(binding or {})
        return result

    def material_read(self, run_id, owner, body, *, consumer="user"):
        self.require_owned(run_id, owner)
        result = self._reader().read(run_id, owner, body, consumer=consumer)
        if consumer == "model":
            result = self._typed(self.store.get("run", run_id), "reading", result)
        return result
