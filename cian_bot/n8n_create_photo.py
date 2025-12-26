from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import requests


class N8NCreatePhotoClient:
    def __init__(self, *, webhook_url: Optional[str] = None, timeout_s: Optional[int] = None) -> None:
        self.webhook_url = (webhook_url or os.getenv("CREATE_PHOTO_WEBHOOK_URL", "")).strip()
        if not self.webhook_url:
            raise RuntimeError("Missing env CREATE_PHOTO_WEBHOOK_URL")
        if timeout_s is None:
            timeout_s = int(os.getenv("CREATE_PHOTO_TIMEOUT_S", "420") or "420")
        self.timeout_s = timeout_s
        self.token = (os.getenv("CREATE_PHOTO_WEBHOOK_TOKEN", "") or "").strip()

    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers: Dict[str, str] = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"

        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = requests.post(self.webhook_url, json=payload, headers=headers, timeout=self.timeout_s)
            except requests.exceptions.RequestException as e:
                last_err = e
                time.sleep(min(2 * attempt, 5))
                continue

            ct = (resp.headers.get("content-type") or "").lower()
            body = (resp.text or "")

            if resp.status_code >= 400:
                last_err = RuntimeError(
                    f"n8n create_photo HTTP {resp.status_code} content-type={ct} body={body[:2000]!r}"
                )
                time.sleep(min(2 * attempt, 5))
                continue
            if "application/json" not in ct:
                last_err = RuntimeError(
                    f"n8n create_photo returned non-JSON. status={resp.status_code} content-type={ct} body={body[:2000]!r}"
                )
                time.sleep(min(2 * attempt, 5))
                continue
            if not body.strip():
                last_err = RuntimeError(
                    f"n8n create_photo returned empty JSON body (attempt {attempt}/3). status={resp.status_code} content-type={ct}"
                )
                time.sleep(min(2 * attempt, 5))
                continue
            try:
                return resp.json()
            except Exception as e:
                last_err = RuntimeError(
                    f"n8n create_photo returned invalid JSON. status={resp.status_code} content-type={ct} body={body[:2000]!r}"
                )
                time.sleep(min(2 * attempt, 5))
                continue

        raise RuntimeError(f"n8n create_photo failed after retries: {last_err}") from last_err


