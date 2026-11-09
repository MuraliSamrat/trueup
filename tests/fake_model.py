"""A scripted chat model for testing agents without calling OpenAI."""
from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    calls: int = 0
    seen: list[list[BaseMessage]] = []
    model_name: str = "scripted"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        msg = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])
