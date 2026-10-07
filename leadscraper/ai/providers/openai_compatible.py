"""Any OpenAI-compatible Chat Completions endpoint: OpenRouter, Groq, Ollama, LM Studio.

``POST {base_url}/chat/completions`` with ``messages``. Structured output
(``response_format: {type: json_schema, json_schema: {name, schema, strict}}``)
is sent only when the entry declares ``structured_output: true``: support
differs per server and per model, and JSON + validation works everywhere.

A local endpoint (localhost, a private address) needs no key and produces no
bill; a remote one is treated like any paid API.
"""

from __future__ import annotations

from typing import Any

from .base import AIRequest, AIResponse, Provider, ProviderError, Refused, is_local_url
from . import http as _http
from .http import post_json


class OpenAICompatibleProvider(Provider):
    kind = "openai_compatible"

    def __init__(self, spec, *, http=None, **kwargs):
        super().__init__(spec, **kwargs)
        self.http = http or _http.session()
        self.bills_separately = not is_local_url(spec.base_url)

    def needs_key(self) -> bool:
        return not is_local_url(self.spec.base_url)

    def problem(self) -> str | None:
        if not self.spec.base_url:
            return "no base_url configured"
        return super().problem()

    def complete(self, request: AIRequest) -> AIResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": request.user}],
            "max_tokens": request.max_tokens,
        }
        structured = request.schema is not None and self.capabilities.structured_output
        if structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": request.schema_name, "schema": request.schema, "strict": True}}
        headers = {"Content-Type": "application/json"}
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = (self.spec.base_url or "").rstrip("/") + "/chat/completions"
        payload = post_json(self, self.http, url, headers=headers, body=body)
        return self.parse(payload, request.model, structured)

    @staticmethod
    def parse(payload: dict[str, Any], model: str, structured: bool = False) -> AIResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("no choices in the response")
        message = choices[0].get("message") or {}
        if message.get("refusal"):
            raise Refused(str(message["refusal"])[:200])
        if choices[0].get("finish_reason") == "content_filter":
            raise Refused("content filter")
        content = message.get("content")
        if isinstance(content, list):          # some servers send content parts
            content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        if not content:
            raise ProviderError("empty message content")
        usage = payload.get("usage") or {}
        return AIResponse(text=str(content), model=str(payload.get("model") or model),
                          input_tokens=int(usage.get("prompt_tokens") or 0),
                          output_tokens=int(usage.get("completion_tokens") or 0),
                          structured=structured)
