"""OpenAI, through the Responses API (``POST {base_url}/responses``).

Structured output is ``text.format = {type: json_schema, name, schema, strict}``;
web search is the ``web_search`` tool, with the consulted URLs requested via
``include: ["web_search_call.action.sources"]`` and cited ones read from
``url_citation`` annotations. A ``refusal`` content item means the model
declined. Verified against the API docs 2026-10-07 (docs/sources.md).
"""

from __future__ import annotations

from typing import Any

from .base import AIRequest, AIResponse, Provider, ProviderError, Refused
from . import http as _http
from .http import post_json


class OpenAIProvider(Provider):
    kind = "openai"

    def __init__(self, spec, *, http=None, **kwargs):
        super().__init__(spec, **kwargs)
        self.http = http or _http.session()

    def _url(self) -> str:
        return (self.spec.base_url or "https://api.openai.com/v1").rstrip("/") + "/responses"

    def complete(self, request: AIRequest) -> AIResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "instructions": request.system,
            "input": request.user,
            "max_output_tokens": request.max_tokens,
        }
        structured = request.schema is not None and not request.web_search \
            and self.capabilities.structured_output
        if structured:
            body["text"] = {"format": {"type": "json_schema", "name": request.schema_name,
                                       "schema": request.schema, "strict": True}}
        if request.web_search:
            body["tools"] = [{"type": "web_search"}]
            body["include"] = ["web_search_call.action.sources"]
        headers = {"Authorization": f"Bearer {self.api_key()}",
                   "Content-Type": "application/json"}
        payload = post_json(self, self.http, self._url(), headers=headers, body=body)
        return self.parse(payload, request.model, structured)

    @staticmethod
    def parse(payload: dict[str, Any], model: str, structured: bool = False) -> AIResponse:
        texts: list[str] = []
        sources: list[str] = []
        searches = 0
        for item in payload.get("output") or []:
            kind = item.get("type")
            if kind == "web_search_call":
                searches += 1
                for source in ((item.get("action") or {}).get("sources") or []):
                    if source.get("url"):
                        sources.append(source["url"])
            elif kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "refusal":
                        raise Refused(str(part.get("refusal") or "refused")[:200])
                    if part.get("type") == "output_text":
                        texts.append(part.get("text") or "")
                        for note in part.get("annotations") or []:
                            if note.get("type") == "url_citation" and note.get("url"):
                                sources.append(note["url"])
        text = "".join(texts) or str(payload.get("output_text") or "")
        if not text:
            status = payload.get("status")
            detail = (payload.get("incomplete_details") or {}).get("reason") or status
            raise ProviderError(f"no output text ({detail})")
        usage = payload.get("usage") or {}
        return AIResponse(text=text, model=str(payload.get("model") or model),
                          input_tokens=int(usage.get("input_tokens") or 0),
                          output_tokens=int(usage.get("output_tokens") or 0),
                          searches=searches, sources=list(dict.fromkeys(sources)),
                          structured=structured)
