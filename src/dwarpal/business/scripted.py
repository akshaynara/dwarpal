"""A deterministic stand-in for Gemini, so any business agent runs offline and on stage.

It reads the gateway's verified task, calls the tool mapped to the task's intent, then reports the
tool's message. The agent is still a real ADK LlmAgent with real tools and session state.
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncGenerator

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types


class ScriptedModel(BaseLlm):
    intent_map: dict[str, str] = {}
    defaults: dict[str, dict[str, Any]] = {}

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        last = llm_request.contents[-1] if llm_request.contents else None
        responses = [p.function_response for p in (last.parts or [])] if last else []
        responses = [r for r in responses if r]
        if responses:
            result = responses[-1].response or {}
            text = result.get("message") or json.dumps(result)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
            return
        task = "".join(p.text or "" for p in (last.parts or [])) if last else ""
        intent = (re.search(r"^intent:\s*(\S+)", task, re.M) or [None, ""])[1]
        params_match = re.search(r"^params:\s*(\{.*\})\s*$", task, re.M)
        params = json.loads(params_match.group(1)) if params_match else {}
        tool = self.intent_map.get(intent)
        if not tool:
            yield LlmResponse(content=types.Content(role="model", parts=[
                types.Part(text="I can't help with that request here.")]))
            return
        args = {**self.defaults.get(tool, {}), **params}
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name=tool, args=args))]))
