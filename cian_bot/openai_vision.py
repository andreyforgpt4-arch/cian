from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import requests


def _extract_json_object(text: str) -> Dict[str, Any]:
    """
    Best-effort parse: find the first JSON object in a text response.
    """
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON object found in response: {text[:200]!r}")
    return json.loads(m.group(0))


PHOTO_SCORE_SCHEMA_NAME = "real_estate_photo_quality"

PHOTO_SCORE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "photo_type",
        "improvement_score_0to100",
        "severity_breakdown",
        "top_issues",
        "should_improve",
        "quick_win_fixes",
        "risk_notes",
    ],
    "properties": {
        "photo_type": {
            "type": "string",
            "enum": ["exterior", "interior", "entrance", "floorplan", "other"],
        },
        "improvement_score_0to100": {"type": "integer", "minimum": 0, "maximum": 100},
        "severity_breakdown": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "lighting_exposure_0to3",
                "white_balance_0to2",
                "sharpness_blur_0to3",
                "noise_compression_0to2",
                "perspective_verticals_0to3",
                "framing_composition_0to2",
                "distractions_clutter_UI_privacy_0to3",
            ],
            "properties": {
                "lighting_exposure_0to3": {"type": "integer", "minimum": 0, "maximum": 3},
                "white_balance_0to2": {"type": "integer", "minimum": 0, "maximum": 2},
                "sharpness_blur_0to3": {"type": "integer", "minimum": 0, "maximum": 3},
                "noise_compression_0to2": {"type": "integer", "minimum": 0, "maximum": 2},
                "perspective_verticals_0to3": {"type": "integer", "minimum": 0, "maximum": 3},
                "framing_composition_0to2": {"type": "integer", "minimum": 0, "maximum": 2},
                "distractions_clutter_UI_privacy_0to3": {"type": "integer", "minimum": 0, "maximum": 3},
            },
        },
        "top_issues": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 10,
        },
        "should_improve": {"type": "string", "enum": ["yes", "maybe", "no"]},
        "quick_win_fixes": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 10,
        },
        "risk_notes": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 10,
        },
    },
}


class OpenAIVisionClient:
    def __init__(self, *, api_key: Optional[str] = None, timeout_s: int = 60) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("Missing env OPENAI_API_KEY")
        self.timeout_s = timeout_s

        self.model = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        self.endpoint = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses").strip()

    def analyze_photo(self, *, image_url: str, prompt: str) -> Dict[str, Any]:
        """
        Returns JSON dict with at least:
          - category: exterior|interior|entrance|floorplan|other
          - score: int 1..10
        """
        body = {
            "model": self.model,
            "temperature": 0.2,
            # Responses API: schema output is configured via text.format (response_format is deprecated).
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": PHOTO_SCORE_SCHEMA_NAME,
                    "strict": True,
                    "schema": PHOTO_SCORE_SCHEMA,
                }
            },
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ],
        }

        resp = requests.post(
            self.endpoint,
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            json=body,
            timeout=self.timeout_s,
        )
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:1000]
            raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {snippet}")
        data = resp.json()

        # Responses API typically has output[].content[].text
        text = ""
        for out in data.get("output", []) or []:
            for c in out.get("content", []) or []:
                if c.get("type") == "output_text" and isinstance(c.get("text"), str):
                    text += c["text"]
        if not text:
            # fallback: some variants include "output_text"
            text = data.get("output_text", "") or ""

        parsed = _extract_json_object(text)
        return parsed


