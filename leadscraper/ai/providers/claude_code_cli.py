"""The Claude Code CLI in ``--print`` mode: your Claude Code subscription, no API key.

Each call spawns a process (~10 s), so features batch their items. The CLI's
own tooling is stripped (``--disallowed-tools *``, no MCP, no settings), which
cuts each call from ~30k context tokens to the prompt itself. It reports a
list-price-equivalent ``total_cost_usd`` that counts against the run's cap; it
is drawn from the subscription allowance, not billed separately.

``--json-schema`` exists (CLI 2.1.x) but is off by default here: it relies on
the CLI's tool machinery, which these calls disable. Set the entry's
``structured_output: true`` to try it; JSON + validation works either way.
"""

from __future__ import annotations

import json
import subprocess
from shutil import which as _which
from typing import Any

from .base import AIRequest, AIResponse, Provider, ProviderError, QuotaExhausted, Refused

#: Text that means "you are out of allowance", not "this request was bad".
QUOTA_MARKERS = (
    "usage limit", "usage_limit", "rate limit", "rate_limit", "quota",
    "limit reached", "limit exceeded", "out of credits", "insufficient credits",
    "too many requests", "upgrade your plan", "resets at", "try again later",
    "429",
)


def is_quota_error(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return any(marker in blob for marker in QUOTA_MARKERS)


class ClaudeCodeCLIProvider(Provider):
    kind = "claude_code_cli"
    bills_separately = False

    def __init__(self, spec, *, run=subprocess.run, **kwargs):
        super().__init__(spec, **kwargs)
        self._run = run

    @property
    def binary(self) -> str:
        return str(self.spec.options.get("binary") or "claude")

    def needs_key(self) -> bool:
        return False

    def key_source(self) -> str | None:
        return "subscription" if _which(self.binary) else None

    def problem(self) -> str | None:
        if self._run is subprocess.run and _which(self.binary) is None:
            return f"the `{self.binary}` CLI isn't on PATH"
        return super().problem()

    def command(self, request: AIRequest) -> list[str]:
        command = [
            self.binary, "-p",
            "--system-prompt", request.system,
            "--model", request.model,
            "--output-format", "json",
            # Without these each call drags in ~30k tokens of tools/skills
            # context and costs ~8x more.
            "--disallowed-tools", "*",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--exclude-dynamic-system-prompt-sections",
        ]
        if request.schema is not None and self.capabilities.structured_output:
            command += ["--json-schema", json.dumps(request.schema)]
        return command

    def complete(self, request: AIRequest) -> AIResponse:
        try:
            completed = self._run(self.command(request), input=request.user,
                                  capture_output=True, text=True, timeout=self.spec.timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"the CLI timed out after {self.spec.timeout:.0f}s") from exc
        except OSError as exc:
            raise ProviderError(f"could not start the CLI: {exc!r}"[:200]) from exc
        if completed.returncode != 0:
            if is_quota_error(completed.stderr, completed.stdout):
                raise QuotaExhausted((completed.stderr or "").strip()[:200]
                                     or "usage limit reached")
            raise ProviderError(f"claude CLI exited {completed.returncode}: "
                                f"{(completed.stderr or '')[:200]}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            if is_quota_error(completed.stdout, completed.stderr):
                raise QuotaExhausted("usage limit reported by the CLI") from None
            raise ProviderError(f"claude CLI returned non-JSON: {completed.stdout[:200]}") from None
        return self.parse(payload, request.model)

    @staticmethod
    def parse(payload: dict[str, Any], model: str) -> AIResponse:
        if payload.get("is_error"):
            detail = str(payload.get("result") or payload)[:300]
            if is_quota_error(detail, str(payload.get("subtype", ""))):
                raise QuotaExhausted(detail)
            if payload.get("stop_reason") == "refusal":
                raise Refused(detail[:200])
            raise ProviderError(f"claude CLI reported an error: {detail}")
        structured = payload.get("structured_output")
        text = json.dumps(structured) if structured is not None else \
            str(payload.get("result") or "")
        usage = payload.get("usage") or {}
        tokens_in = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return AIResponse(text=text, model=model, input_tokens=tokens_in,
                          output_tokens=int(usage.get("output_tokens") or 0),
                          reported_cost=float(payload.get("total_cost_usd") or 0.0),
                          structured=structured is not None)
