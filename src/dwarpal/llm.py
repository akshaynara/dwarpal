"""Thin Gemini helper used by the gateway's own checks (the business agent uses ADK directly)."""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings

log = logging.getLogger("dwarpal.llm")


class GeminiJudge:
    """Asks Gemini for a small JSON verdict. Returns None on any failure, so callers fall back."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        if settings.use_gemini:
            try:
                from google import genai

                self._client = genai.Client()
            except Exception as exc:  # noqa: BLE001
                log.warning("Gemini client unavailable, falling back to rules only: %s", exc)

    @property
    def available(self) -> bool:
        return self._client is not None

    async def json_verdict(self, system: str, prompt: str) -> dict[str, Any] | None:
        if self._client is None:
            return None
        try:
            from google.genai import types

            resp = await self._client.aio.models.generate_content(
                model=self.settings.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.0,
                    max_output_tokens=300,
                ),
            )
            return json.loads(resp.text or "{}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Gemini judge failed, using rules only: %s", exc)
            return None
