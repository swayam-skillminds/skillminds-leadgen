"""Mocked HTTP for the AI providers (Phase 5): no test ever reaches a real one.

* :class:`FakeHTTP` -- a ``requests`` transport adapter: the OpenAI, Gemini and
  OpenAI-compatible providers send their real requests through it, and it
  answers from a responder function. Every request is recorded (URL, headers,
  JSON body) so tests can check the wire format.
* :func:`anthropic_client` -- an ``httpx2`` client with a mock transport for the
  official Anthropic SDK (the SDK builds and parses the real messages).
* :func:`llm` -- a scripted OpenAI-compatible endpoint for feature and run
  tests: it answers each call from the request's system prompt and user text.
* Response builders in each API's real shape.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict


class Recorded:
    def __init__(self, method: str, url: str, headers: dict, body: Any):
        self.method, self.url, self.headers, self.body = method, url, headers, body


class FakeHTTP(BaseAdapter):
    """Answers every request with ``responder(recorded) -> (status, payload[, headers])``."""

    def __init__(self, responder: Callable[[Recorded], tuple]):
        super().__init__()
        self.responder = responder
        self.requests: list[Recorded] = []

    def send(self, request, **kwargs):
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body
        recorded = Recorded(request.method, request.url, dict(request.headers), parsed)
        self.requests.append(recorded)
        answer = self.responder(recorded)
        status, payload = answer[0], answer[1]
        headers = answer[2] if len(answer) > 2 else {}
        response = requests.Response()
        response.status_code = status
        if isinstance(payload, bytes):
            response._content = payload
        elif isinstance(payload, str):
            response._content = payload.encode()
        else:
            response._content = json.dumps(payload).encode()
        response.headers = CaseInsensitiveDict({"content-type": "application/json", **headers})
        response.url = request.url
        response.request = request
        response.encoding = "utf-8"
        return response

    def close(self):
        pass


def session_with(adapter: FakeHTTP) -> requests.Session:
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def install_http(monkeypatch, adapter: FakeHTTP) -> FakeHTTP:
    """Every requests-based provider built from now on talks to `adapter`."""
    from leadscraper.ai.providers import http as provider_http

    monkeypatch.setattr(provider_http, "session", lambda: session_with(adapter))
    return adapter


def anthropic_client(handler: Callable[[Any], Any]):
    """An httpx2 client whose transport answers with ``handler(request) -> httpx2.Response``."""
    import anthropic
    import httpx2

    return anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))


# --- responses in each API's shape ------------------------------------------------------------

def openai_response(text: str = "", *, sources: tuple[str, ...] = (), citations=(),
                    refusal: str | None = None, tokens=(100, 20), model="gpt-6-luna"):
    output: list[dict] = []
    for url in sources:
        output.append({"type": "web_search_call", "id": "ws_1", "status": "completed",
                       "action": {"type": "search", "query": "q",
                                  "sources": [{"type": "url", "url": url}]}})
    content = [{"type": "refusal", "refusal": refusal}] if refusal else [
        {"type": "output_text", "text": text,
         "annotations": [{"type": "url_citation", "url": u, "title": "t", "start_index": 0,
                          "end_index": 1} for u in citations]}]
    output.append({"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                   "content": content})
    return {"id": "resp_1", "object": "response", "status": "completed", "model": model,
            "output": output,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                      "total_tokens": sum(tokens)}}


def chat_response(text: str, *, tokens=(80, 15), model="local-model", refusal=None):
    message = {"role": "assistant", "content": None if refusal else text}
    if refusal:
        message["refusal"] = refusal
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


def gemini_response(text: str = "", *, chunks=(), queries=(), tokens=(90, 12, 0),
                    finish="STOP", block=None):
    if block:
        return {"promptFeedback": {"blockReason": block}}
    candidate: dict = {"content": {"role": "model", "parts": [{"text": text}]},
                       "finishReason": finish}
    if chunks or queries:
        candidate["groundingMetadata"] = {
            "webSearchQueries": list(queries),
            "groundingChunks": [{"web": {"uri": uri, "title": title}} for uri, title in chunks]}
    return {"candidates": [candidate], "modelVersion": "gemini-3.5-flash-lite",
            "usageMetadata": {"promptTokenCount": tokens[0], "candidatesTokenCount": tokens[1],
                              "thoughtsTokenCount": tokens[2]}}


def anthropic_message(text: str = "", *, model="claude-haiku-4-5", tokens=(120, 30),
                      stop_reason="end_turn", search_results=(), searches=0):
    content: list[dict] = []
    if search_results:
        content.append({"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                        "input": {"query": "q"}})
        content.append({"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
                        "content": [{"type": "web_search_result", "url": url, "title": "t",
                                     "encrypted_content": "x", "page_age": None}
                                    for url in search_results]})
    if stop_reason != "refusal":
        content.append({"type": "text", "text": text})
    usage: dict = {"input_tokens": tokens[0], "output_tokens": tokens[1]}
    if searches:
        usage["server_tool_use"] = {"web_search_requests": searches}
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": usage}


# --- a scripted model behind an OpenAI-compatible endpoint ------------------------------------

LOCAL_URL = "http://localhost:11434/v1"


def local_provider(name: str = "local", **extra) -> dict:
    """A provider entry for a local OpenAI-compatible endpoint (no key, no bill)."""
    return {"name": name, "kind": "openai_compatible", "base_url": LOCAL_URL,
            "worker_model": "test-worker", "judge_model": "test-judge", "retries": 0, **extra}


def llm(monkeypatch, answer: Callable[[str, str], Any]) -> FakeHTTP:
    """Every OpenAI-compatible call is answered by ``answer(system, user)``.

    ``answer`` returns a dict/list (sent as the JSON text), a string (sent as is),
    or ``(status, payload)`` for an HTTP error.
    """
    def responder(req: Recorded):
        messages = (req.body or {}).get("messages") or []
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        result = answer(system, user)
        if isinstance(result, tuple):
            return result
        text = result if isinstance(result, str) else json.dumps(result)
        return 200, chat_response(text, model=(req.body or {}).get("model", "m"))

    return install_http(monkeypatch, FakeHTTP(responder))


def fake_cli(payload: dict | None = None, *, returncode: int = 0, stderr: str = "",
             calls: list | None = None):
    """A stand-in for ``subprocess.run`` of the claude CLI."""
    import subprocess

    def run(command, *, input=None, capture_output=True, text=True, timeout=None):
        if calls is not None:
            calls.append({"command": command, "input": input, "timeout": timeout})
        return subprocess.CompletedProcess(command, returncode,
                                           stdout=json.dumps(payload or {}), stderr=stderr)
    return run
