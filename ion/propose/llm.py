"""LLM backends. All output is untrusted text and is schema-validated by the caller."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            text = text[a:b + 1]
    return json.loads(text)


class LLM:
    """Base: counts calls and enforces the cap."""

    def __init__(self, cfg):
        self.cfg = cfg["llm"]
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return self.cfg["backend"] != "none" and self.calls < self.cfg["max_calls_per_run"]

    def model_for(self, structural: bool) -> str:
        return self.cfg["model_structural" if structural else "model_default"]

    def complete_json(self, prompt: str, structural: bool = False) -> dict:
        if not self.enabled:
            raise LLMError("llm disabled or call cap reached")
        self.calls += 1
        return extract_json(self._complete(prompt, self.model_for(structural)))

    def _complete(self, prompt: str, model: str) -> str:
        raise NotImplementedError


class NoneLLM(LLM):
    @property
    def enabled(self) -> bool:
        return False


class ClaudeCLI(LLM):
    def _complete(self, prompt: str, model: str) -> str:
        # The API key belongs to the anthropic_api backend. Inherited here it overrides the
        # CLI's own seat login, silently degrading every call to templates (seen live).
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        p = subprocess.run(["claude", "-p", "--output-format", "json", "--model", model], input=prompt,
                           capture_output=True, text=True, timeout=self.cfg["timeout_s"], env=env)
        if p.returncode != 0:
            raise LLMError(f"claude exited {p.returncode}: {p.stderr.strip()[:200]}")
        try:
            return json.loads(p.stdout)["result"]
        except (ValueError, KeyError) as e:
            raise LLMError("unparseable claude output") from e


class AnthropicAPI(LLM):
    def _complete(self, prompt: str, model: str) -> str:
        import os
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY not set")
        model = self.cfg.get("api_models", {}).get(model, model)
        body = json.dumps({"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg["timeout_s"]) as r:
                data = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"api error: {type(e).__name__}") from e
        return "".join(b.get("text", "") for b in data.get("content", []))


def make_llm(cfg) -> LLM:
    return {"claude_cli": ClaudeCLI, "anthropic_api": AnthropicAPI, "none": NoneLLM}[cfg["llm"]["backend"]](cfg)
