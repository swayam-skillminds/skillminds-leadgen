"""Anthropic's Messages API, through the official ``anthropic`` SDK (extra ``ai``).

* Structured output: ``output_config.format = {type: json_schema, schema}``.
* Web search: the server-side ``web_search`` tool (``web_search_20260209`` on the
  models with dynamic filtering, ``web_search_20250305`` on older ones such as
  Haiku 4.5). Result URLs come from ``web_search_tool_result`` blocks and
  ``web_search_result_location`` citations; a ``pause_turn`` is resumed twice at
  most.
* Claude Opus 5.5 / Opus 5 / Sonnet 5.5 calls opt into server-side refusal
  fallbacks (``fallbacks: "default"``): a declined request is re-run on a
  fallback model inside the same call. A refusal that survives raises ``Refused``.
* The SDK's own retries (429, 5xx, connection) run first; what is left maps onto
  the ``base`` errors.

Tests pass an ``httpx2`` client with a mock transport (``http_client``), so the
SDK's real request building runs with no network.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import (
    AIRequest, AIResponse, AuthFailed, NotConfigured, Provider, ProviderError, QuotaExhausted,
    Refused,
)

log = logging.getLogger(__name__)

#: Models whose web search tool has dynamic filtering (the newer tool version).
_DYNAMIC_SEARCH = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                   "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable")
#: Models that take the server-side refusal fallback ("default" routing).
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MAX_CONTINUATIONS = 2

#: Tests set this to a function returning an ``httpx2`` client with a mock transport.
http_client_factory = None


def search_tool(model: str, max_uses: int) -> dict[str, Any]:
    version = ("web_search_20260209" if model.startswith(_DYNAMIC_SEARCH)
               else "web_search_20250305")
    return {"type": version, "name": "web_search", "max_uses": max_uses}


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, spec, *, http_client=None, **kwargs):
        super().__init__(spec, **kwargs)
        self._http_client = http_client
        self._client = None

    def problem(self) -> str | None:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return "the `anthropic` package isn't installed (pip install 'leadscraper[ai]')"
        return super().problem()

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise NotConfigured("the `anthropic` package isn't installed") from exc
            kwargs: dict[str, Any] = {"api_key": self.api_key(), "timeout": self.spec.timeout,
                                      "max_retries": self.spec.retries}
            if self.spec.base_url:
                kwargs["base_url"] = self.spec.base_url
            http_client = self._http_client or (http_client_factory()
                                                if http_client_factory else None)
            if http_client is not None:
                kwargs["http_client"] = http_client
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def complete(self, request: AIRequest) -> AIResponse:
        import anthropic

        client = self._get_client()
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }
        structured = request.schema is not None and not request.web_search \
            and self.capabilities.structured_output
        if structured:
            params["output_config"] = {"format": {"type": "json_schema",
                                                  "schema": request.schema}}
        if request.web_search:
            params["tools"] = [search_tool(request.model, request.max_searches)]
        fallback = request.model.startswith(_FALLBACK_MODELS) and not self.spec.base_url

        try:
            response = self._create(client, params, fallback)
            blocks = list(response.content)
            continuations = 0
            while response.stop_reason == "pause_turn" and continuations < _MAX_CONTINUATIONS:
                continuations += 1
                params["messages"] = [params["messages"][0],
                                      {"role": "assistant", "content": response.content}]
                response = self._create(client, params, fallback)
                blocks.extend(response.content)
        except anthropic.AuthenticationError as exc:
            raise AuthFailed(_short(exc)) from None
        except anthropic.PermissionDeniedError as exc:
            raise AuthFailed(_short(exc)) from None
        except anthropic.RateLimitError as exc:
            # The SDK has already retried with back-off. A rate limit is not an
            # empty account: skip this call, keep the provider for the next.
            text = _short(exc)
            if any(w in text.lower() for w in ("credit", "billing", "quota")):
                raise QuotaExhausted(text) from None
            raise ProviderError(text) from None
        except anthropic.BadRequestError as exc:
            text = _short(exc)
            if "credit balance" in text.lower() or "billing" in text.lower():
                raise QuotaExhausted(text) from None
            raise ProviderError(text) from None
        except anthropic.APIStatusError as exc:
            raise ProviderError(_short(exc)) from None
        except anthropic.APIError as exc:          # connection, timeout
            raise ProviderError(_short(exc)) from None
        return self.parse(response, blocks, request.model, structured)

    @staticmethod
    def _create(client, params: dict[str, Any], fallback: bool):
        if fallback:
            return client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default",
                                               **params)
        return client.messages.create(**params)

    @staticmethod
    def parse(response: Any, blocks: list, model: str, structured: bool) -> AIResponse:
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise Refused(f"refused ({category or 'no category'})")
        texts: list[str] = []
        sources: list[str] = []
        for block in blocks:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text or "")
                for citation in getattr(block, "citations", None) or []:
                    url = getattr(citation, "url", None)
                    if url:
                        sources.append(url)
            elif kind == "web_search_tool_result":
                content = getattr(block, "content", None)
                if isinstance(content, list):          # an error is an object, not a list
                    sources.extend(r.url for r in content if getattr(r, "url", None))
        text = "".join(texts)
        if not text:
            raise ProviderError(f"no text in the response (stop reason {response.stop_reason})")
        usage = response.usage
        server = getattr(usage, "server_tool_use", None)
        searches = int(getattr(server, "web_search_requests", 0) or 0) if server else 0
        return AIResponse(text=text, model=str(getattr(response, "model", "") or model),
                          input_tokens=int(usage.input_tokens or 0),
                          output_tokens=int(usage.output_tokens or 0), searches=searches,
                          sources=list(dict.fromkeys(sources)), structured=structured)


def _short(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    return (f"HTTP {status}: " if status else "") + str(message)[:200]
