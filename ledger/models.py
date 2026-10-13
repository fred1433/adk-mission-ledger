"""Model selection. Fixture by default; live Gemini only behind two switches.

FixtureLlm is an ADK BaseLlm that replays a scripted sequence of model turns
from fixtures/model/<kind>.json. Turn i is chosen by counting the model turns
already present in the request history, so a fresh process resuming a
suspended invocation picks up exactly where the old one stopped. These turns
are written by hand to exercise control flow; they were not captured from
Gemini, and nothing in this repository presents them as model judgement.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import AsyncGenerator

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .config import REPO_ROOT, settings

TEMPLATE = re.compile(r"\$\{(mission|last)\.([a-zA-Z0-9_.]+)\}")


class FixtureLlm(BaseLlm):
    mission: dict = {}
    script: list = []

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)

    async def generate_content_async(self, llm_request: LlmRequest,
                                     stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        contents = llm_request.contents or []
        turn = sum(1 for c in contents if c.role == "model")
        last = _last_function_response(contents)
        if turn >= len(self.script):
            raise RuntimeError(f"fixture {self.model} has no turn {turn}")
        step = self.script[turn]
        if "text" in step:
            part = types.Part(text=_fill(step["text"], self.mission, last))
        else:
            fc = step["call"]
            part = types.Part(function_call=types.FunctionCall(
                name=fc["name"], args=_fill(fc.get("args", {}), self.mission, last)))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


_LIVE_CALLS = {"n": 0}


def _capped_gemini_cls():
    from google.adk.models.google_llm import Gemini

    class _Capped(Gemini):
        """Live Gemini with a hard per-process call budget and bounded output."""

        async def generate_content_async(self, llm_request, stream=False):
            cap = settings().max_model_calls
            if _LIVE_CALLS["n"] >= cap:
                raise RuntimeError(f"live model call budget exhausted ({cap} per process)")
            _LIVE_CALLS["n"] += 1
            cfg = llm_request.config
            if cfg is not None:
                cfg.max_output_tokens = min(cfg.max_output_tokens or 1024, 1024)
                cfg.temperature = 0
            async for r in super().generate_content_async(llm_request, stream):
                yield r

    return _Capped


def CappedGemini(**kw):  # noqa: N802 - factory keeps the paid import lazy
    return _capped_gemini_cls()(**kw)


def _last_function_response(contents) -> dict:
    for c in reversed(contents):
        for p in c.parts or []:
            if p.function_response is not None:
                return {"name": p.function_response.name, "response": p.function_response.response or {}}
    return {}


def _fill(value, mission: dict, last: dict):
    if isinstance(value, str):
        def sub(m):
            root = mission if m.group(1) == "mission" else last
            cur = root
            for key in m.group(2).split("."):
                cur = cur.get(key, "") if isinstance(cur, dict) else ""
            return str(cur)
        return TEMPLATE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _fill(v, mission, last) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, mission, last) for v in value]
    return value


def for_mission(mission: dict):
    """Return (model, descriptor). The descriptor is pinned in the manifest."""
    s = settings()
    if s.paid_models_enabled:
        return CappedGemini(model=s.live_model), {"mode": "live", "model": s.live_model,
                                                  "max_calls_per_process": s.max_model_calls}
    path = REPO_ROOT / "fixtures" / "model" / f"{mission['kind']}.json"
    raw = path.read_bytes()
    data = json.loads(raw)
    sha = hashlib.sha256(raw).hexdigest()[:12]
    model = FixtureLlm(model=f"fixture:{path.name}@{sha}", mission=mission, script=data["turns"])
    return model, {"mode": "fixture", "model": model.model}
