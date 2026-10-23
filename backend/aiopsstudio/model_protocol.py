"""Bounded, attempt-local provider protocol. Never a durable history source."""
from __future__ import annotations

import copy
import json
import uuid
from contextvars import ContextVar

from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _convert_message_to_dict


ACTIVE_TOOL_TURN_POLICY: ContextVar[tuple[str, str, str] | None] = ContextVar("active_tool_turn_policy", default=None)
MAX_TOOL_TURN_BYTES = 48 * 1024
MAX_TOOL_TURNS = 2


def protocol_messages(messages):
    """Mirror the pinned Chat Completions serializer, including its null content."""
    result = []
    for message in messages:
        item = _convert_message_to_dict(message)
        if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
            reasoning = message.additional_kwargs["reasoning_content"]
            if not isinstance(reasoning, str):
                raise ToolTurnError("供应商推理协议格式不受支持")
            item["reasoning_content"] = reasoning
        result.append(item)
    return result


def protocol_bytes(messages):
    # Default separators/ASCII escaping intentionally match the conservative
    # SDK payload estimate, including provider reasoning and tool arguments.
    return len(json.dumps(protocol_messages(messages), ensure_ascii=True).encode())


class CompatibleChatOpenAI(ChatOpenAI):
    """Retain only the explicitly supported provider field; no policy bypass.

    Runtime uses non-streaming Chat Completions. Unknown provider fields remain
    unsupported, and the actual HTTP guard must allow reasoning_content before
    any replay. This adapter neither enables reasoning nor grants that field.
    """

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw.get("choices", []), strict=True):
            message = choice.get("message", {})
            if "reasoning_content" in message and message["reasoning_content"] is not None:
                if not isinstance(message["reasoning_content"], str):
                    raise ToolTurnError("供应商推理协议格式不受支持")
                generation.message.additional_kwargs["reasoning_content"] = message["reasoning_content"]
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        if "messages" in payload:
            for message, item in zip(messages, payload["messages"], strict=True):
                if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
                    reasoning = message.additional_kwargs["reasoning_content"]
                    if not isinstance(reasoning, str):
                        raise ToolTurnError("供应商推理协议格式不受支持")
                    item["reasoning_content"] = reasoning
        return payload


class ToolTurnError(ValueError):
    """A fixed public reason to stop exploration, never provider body text."""


class AttemptToolTurns:
    """Own only responses observed during this one running investigation.

    Tool content was already projected under its material type. Binding the
    *whole* policy before the originating model request ensures that a later
    field drop cannot turn that old body into an allowed generic text string.
    """

    def __init__(self, run_id, attempt_id, *, max_bytes=MAX_TOOL_TURN_BYTES, max_groups=MAX_TOOL_TURNS):
        self.scope = (run_id, attempt_id)
        self.max_bytes, self.max_groups = max_bytes, max_groups
        self.groups = []
        self.pending = None

    def _check(self, scope, digest):
        if tuple(scope) != self.scope:
            raise ToolTurnError("工具协议不属于当前尝试，停止重放")
        if any(group[0] != digest for group in self.groups) or (self.pending and self.pending[0] != digest):
            raise ToolTurnError("授权策略已变化，停止重放旧工具协议并重新投影报告")

    def capture(self, messages, scope, digest, visible_text):
        self._check(scope, digest)
        if self.pending is not None:
            raise ToolTurnError("上一工具回合尚未完整返回")
        clean = []
        for message in messages:
            if not isinstance(message, AIMessage):
                raise ToolTurnError("模型响应包含不受支持的协议角色")
            message_id = message.id or "tool-turn-" + uuid.uuid4().hex
            if message.tool_calls:
                ids = [call.get("id") for call in message.tool_calls]
                if self.pending or message.invalid_tool_calls or not all(isinstance(i, str) and i for i in ids) or len(ids) != len(set(ids)):
                    raise ToolTurnError("工具请求标识无效或重复")
                if not isinstance(message.content, str):
                    raise ToolTurnError("供应商工具正文格式不受支持")
                kwargs = ({"reasoning_content": message.additional_kwargs["reasoning_content"]}
                          if "reasoning_content" in message.additional_kwargs else {})
                protocol = AIMessage(content=message.content, id=message_id,
                    tool_calls=copy.deepcopy(message.tool_calls), additional_kwargs=kwargs)
                if protocol_bytes([protocol]) > self.max_bytes:
                    raise ToolTurnError("最新工具协议超过阅读上限，停止探索")
                self.pending = (digest, protocol)
            # Reconstruct, rather than copy unknown provider metadata. This is
            # the only response the graph/checkpointer and progress UI receive.
            clean.append(AIMessage(content=visible_text(message.content), id=message_id,
                tool_calls=copy.deepcopy(message.tool_calls), invalid_tool_calls=copy.deepcopy(message.invalid_tool_calls),
                usage_metadata=copy.deepcopy(message.usage_metadata)))
        return clean

    def messages(self, graph_messages, scope, digest):
        self._check(scope, digest)
        if self.pending:
            bound_policy, assistant = self.pending
            expected = [call["id"] for call in assistant.tool_calls]
            matches = [i for i, message in enumerate(graph_messages)
                       if isinstance(message, AIMessage) and message.id == assistant.id
                       and message.tool_calls == assistant.tool_calls]
            if len(matches) != 1:
                raise ToolTurnError("当前工具请求未找到唯一对应回合")
            replies = graph_messages[matches[0] + 1:]
            if (len(replies) != len(expected) or not all(isinstance(message, ToolMessage) for message in replies)
                    or sorted(message.tool_call_id for message in replies) != sorted(expected)
                    or not all(isinstance(message.content, str) for message in replies)):
                raise ToolTurnError("当前工具回合返回不完整，停止探索")
            indexed = {message.tool_call_id: message for message in replies}
            group = [assistant, *(ToolMessage(content=indexed[key].content, tool_call_id=key) for key in expected)]
            if protocol_bytes(group) > self.max_bytes:
                raise ToolTurnError("最新完整工具回合超过阅读上限，停止探索")
            self.groups.append((bound_policy, group))
            self.pending = None
            while len(self.groups) > self.max_groups or protocol_bytes([m for _, group in self.groups for m in group]) > self.max_bytes:
                self.groups.pop(0)
        return [message.model_copy(deep=True) for _, group in self.groups for message in group]
