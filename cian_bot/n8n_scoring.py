from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests


class N8NPhotoScoringClient:
    def __init__(self, *, webhook_url: Optional[str] = None, timeout_s: Optional[int] = None) -> None:
        self.webhook_url = (webhook_url or os.getenv("PHOTO_SCORING_WEBHOOK_URL", "")).strip()
        if not self.webhook_url:
            raise RuntimeError("Missing env PHOTO_SCORING_WEBHOOK_URL")
        # Default timeout: 300 seconds (5 minutes) to handle n8n queue delays
        # Can be overridden via PHOTO_SCORING_TIMEOUT_S env var or timeout_s parameter
        if timeout_s is None:
            timeout_s = int(os.getenv("PHOTO_SCORING_TIMEOUT_S", "300") or "300")
        self.timeout_s = timeout_s
        self.token = (os.getenv("PHOTO_SCORING_WEBHOOK_TOKEN", "") or "").strip()

    def score(self, *, offer_id: int, idx: int, photo_url: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "offer_id": offer_id,
            "photo_index": idx,
            "photo_url": photo_url,
        }
        headers: Dict[str, str] = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"

        # n8n sometimes returns 200 with empty body (transient). Retry a couple times.
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            resp = requests.post(self.webhook_url, json=payload, headers=headers, timeout=self.timeout_s)
            ct = (resp.headers.get("content-type") or "").lower()
            body = (resp.text or "")
            if resp.status_code >= 400:
                last_err = RuntimeError(
                    f"n8n webhook HTTP {resp.status_code} content-type={ct} body={body[:2000]!r}"
                )
                continue
            if "application/json" not in ct:
                last_err = RuntimeError(
                    f"n8n webhook returned non-JSON. status={resp.status_code} content-type={ct} body={body[:2000]!r}"
                )
                continue
            if not body.strip():
                last_err = RuntimeError(
                    f"n8n webhook returned empty JSON body (attempt {attempt}/3). status={resp.status_code} content-type={ct}"
                )
                continue
            try:
                return resp.json()
            except Exception as e:
                last_err = RuntimeError(
                    f"n8n webhook returned invalid JSON. status={resp.status_code} content-type={ct} body={body[:2000]!r}"
                )
                continue

        raise last_err or RuntimeError("n8n webhook failed")


