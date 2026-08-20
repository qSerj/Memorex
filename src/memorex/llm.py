from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from openai import OpenAI
from pydantic import BaseModel

from memorex.config import LLMConfig
from memorex.domain import ModelCallResult


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        messages: Sequence[dict[str, str]],
        response_model: type[BaseModel],
        schema_name: str,
    ) -> ModelCallResult: ...


class OpenAICompatibleProvider:
    """Strict JSON Schema client for OpenAI-compatible Chat Completions servers."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "not-required",
            max_retries=0,
            timeout=120.0,
        )

    def complete(
        self,
        *,
        messages: Sequence[dict[str, str]],
        response_model: type[BaseModel],
        schema_name: str,
    ) -> ModelCallResult:
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": response_model.model_json_schema(),
            },
        }
        completion = self.client.chat.completions.create(
            model=self.config.model,
            messages=list(messages),  # type: ignore[arg-type]
            response_format=response_format,  # type: ignore[arg-type]
        )
        message = completion.choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise RuntimeError(f"Model refused the request: {refusal}")
        if not message.content:
            raise RuntimeError("Model returned no structured content")
        usage = completion.usage
        return ModelCallResult(
            raw_output=message.content,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )
