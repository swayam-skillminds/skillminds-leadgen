"""Google Gemini, through ``models.generateContent`` (v1beta REST).

``POST {base}/v1beta/models/{model}:generateContent`` with the key in
``x-goog-api-key``. Structured output is ``generationConfig.responseMimeType =
application/json`` plus ``responseSchema`` (Gemini's OpenAPI subset, see
``ai.schema.gemini_schema``); web search is the ``googleSearch`` tool, whose
sources come back in ``groundingMetadata.groundingChunks[].web``.

Grounding chunk URIs are Google redirect links, not the publisher's URL; their
``title`` is the source's domain. Both are kept in ``sources``, and the web-search
discovery source checks a returned organisation against the domains -- after
fetching its page itself. Verified against the API reference 2026-10-07.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..schema import gemini_schema
from .base import AIRequest, AIResponse, Provider, ProviderError, Refused
from . import http as _http
from .http import post_json

_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION",
            "IMAGE_SAFETY", "LANGUAGE"}


class GeminiProvider(Provider):
    kind = "gemini"

    def __init__(self, spec, *, http=None, **kwargs):
        super().__init__(spec, **kwargs)
        self.http = http or _http.session()

    def complete(self, request: AIRequest) -> AIResponse:
        config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
        structured = request.schema is not None and not request.web_search \
            and self.capabilities.structured_output
        if structured:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = gemini_schema(request.schema)
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": config,
        }
        if request.web_search:
            body["tools"] = [{"googleSearch": {}}]
        base = (self.spec.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        url = f"{base}/v1beta/models/{quote(request.model, safe='.-_')}:generateContent"
        headers = {"x-goog-api-key": self.api_key() or "", "Content-Type": "application/json"}
        payload = post_json(self, self.http, url, headers=headers, body=body)
        return self.parse(payload, request.model, structured)

    @staticmethod
    def parse(payload: dict[str, Any], model: str, structured: bool = False) -> AIResponse:
        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise Refused(f"prompt blocked: {feedback['blockReason']}")
        candidates = payload.get("candidates") or []
        if not candidates:
            raise ProviderError("no candidates in the response")
        first = candidates[0]
        if first.get("finishReason") in _BLOCKED:
            raise Refused(f"finish reason {first['finishReason']}")
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text") or "") for p in parts if not p.get("thought"))
        if not text:
            raise ProviderError(f"no text (finish reason {first.get('finishReason')})")
        grounding = first.get("groundingMetadata") or {}
        sources: list[str] = []
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            for value in (web.get("uri"), web.get("title")):
                if value:
                    sources.append(str(value))
        usage = payload.get("usageMetadata") or {}
        output = int(usage.get("candidatesTokenCount") or 0) + \
            int(usage.get("thoughtsTokenCount") or 0)     # thinking is billed as output
        return AIResponse(text=text, model=str(payload.get("modelVersion") or model),
                          input_tokens=int(usage.get("promptTokenCount") or 0),
                          output_tokens=output,
                          searches=len(grounding.get("webSearchQueries") or []),
                          sources=list(dict.fromkeys(sources)), structured=structured)
