"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """All knobs in one place.

    LLM mode:
      - "gemini": use Gemini through the Gemini API (GOOGLE_API_KEY) or Vertex AI
        (GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT).
      - "mock": deterministic, offline. The clinic agent still runs on ADK, driven by a
        scripted model, so the demo never fails on stage or without a network.
      - "auto" (default): gemini when credentials are present, otherwise mock.
    """

    llm_mode: str = field(default_factory=lambda: os.getenv("DWARPAL_LLM_MODE", "auto"))
    model: str = field(default_factory=lambda: os.getenv("DWARPAL_MODEL", "gemini-flash-latest"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DWARPAL_DATA_DIR", ".dwarpal")))
    policy_path: Path | None = field(
        default_factory=lambda: Path(p) if (p := os.getenv("DWARPAL_POLICY")) else None
    )
    public_url: str = field(default_factory=lambda: os.getenv("DWARPAL_PUBLIC_URL", "http://localhost:8080"))
    enable_demo_endpoints: bool = field(
        default_factory=lambda: os.getenv("DWARPAL_DEMO", "true").lower() != "false"
    )
    model_armor_template: str | None = field(default_factory=lambda: os.getenv("DWARPAL_MODEL_ARMOR_TEMPLATE"))

    @property
    def resolved_llm_mode(self) -> str:
        mode = self.llm_mode.lower()
        if mode in {"gemini", "mock"}:
            return mode
        has_key = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
        has_vertex = _truthy(os.getenv("GOOGLE_GENAI_USE_VERTEXAI")) and bool(os.getenv("GOOGLE_CLOUD_PROJECT"))
        return "gemini" if (has_key or has_vertex) else "mock"

    @property
    def use_gemini(self) -> bool:
        return self.resolved_llm_mode == "gemini"
