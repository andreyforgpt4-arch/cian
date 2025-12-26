from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from cian_bot.storage_state import (
    build_requests_cookie_jar,
    cookies_for_domains,
    load_storage_state,
)


SEARCH_OFFERS_URL = "https://api.cian.ru/search-offers/v2/search-offers-desktop/"


@dataclass(frozen=True)
class CianOffer:
    offer_id: int
    cian_url: Optional[str]
    photos: List[str]
    seller_first_name: Optional[str]
    offer_description: Optional[str]
    raw: Dict[str, Any]


def _guess_cian_url(offer: Dict[str, Any]) -> Optional[str]:
    # Cian response shapes vary. Try most common keys.
    for key in ("fullUrl", "cianUrl", "url"):
        v = offer.get(key)
        if isinstance(v, str) and v:
            return v
    # Nested
    for path in (("links", "cianUrl"), ("links", "fullUrl"), ("seo", "path")):
        cur: Any = offer
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur:
            # seo.path can be relative
            return cur if cur.startswith("http") else f"https://www.cian.ru{cur}"
    return None


def _extract_photos(offer: Dict[str, Any]) -> List[str]:
    photos: List[str] = []
    # Common: offer["photos"] is list of {fullUrl/url}
    for key in ("photos", "photo", "images"):
        v = offer.get(key)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    for k in ("fullUrl", "url", "originalUrl", "previewUrl"):
                        u = item.get(k)
                        if isinstance(u, str) and u.startswith("http"):
                            photos.append(u)
                            break
                elif isinstance(item, str) and item.startswith("http"):
                    photos.append(item)
    # Dedup keep order
    seen = set()
    out = []
    for p in photos:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _first_name_only(full: str | None) -> Optional[str]:
    if not full:
        return None
    full = full.strip()
    if not full:
        return None
    return re.split(r"\s+", full)[0].strip() or None


def _extract_description(offer: Dict[str, Any]) -> Optional[str]:
    for key in ("description", "offerDescription"):
        v = offer.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # sometimes nested
    for path in (("description", "text"), ("content", "description")):
        cur: Any = offer
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return None


def _extract_seller_first_name(offer: Dict[str, Any]) -> Optional[str]:
    # Common nested objects: agent, user, seller, offerOwner
    for obj_key in ("agent", "user", "seller", "offerOwner", "owner"):
        obj = offer.get(obj_key)
        if isinstance(obj, dict):
            for name_key in ("name", "fullName", "firstName"):
                v = obj.get(name_key)
                if isinstance(v, str) and v.strip():
                    return _first_name_only(v)
    # Sometimes name is directly on offer
    for key in ("userName", "agentName", "sellerName"):
        v = offer.get(key)
        if isinstance(v, str) and v.strip():
            return _first_name_only(v)
    return None


def _extract_offers(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Most common: data["data"]["offersSerialized"] or data["offersSerialized"]
    candidates = []
    for path in (("data", "offersSerialized"), ("offersSerialized",), ("data", "offers"), ("offers",)):
        cur: Any = data
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and isinstance(cur, list):
            candidates = cur
            break
    # Each item can be dict or serialized JSON string
    offers: List[Dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict):
            offers.append(item)
        elif isinstance(item, str):
            try:
                offers.append(json.loads(item))
            except Exception:
                continue
    return offers


class CianApi:
    def __init__(self, *, storage_state_path: Optional[Path] = None, timeout_s: int = 30) -> None:
        self.session = requests.Session()
        self.timeout_s = timeout_s
        self.storage_state_path = storage_state_path

        origin = os.getenv("CIAN_ORIGIN", "https://www.cian.ru").strip() or "https://www.cian.ru"
        referer = os.getenv("CIAN_REFERER", origin + "/").strip() or (origin + "/")

        # Default headers that usually work for Cian API.
        self.session.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json;charset=UTF-8",
                "origin": origin,
                "referer": referer,
                "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )

        if storage_state_path and storage_state_path.exists():
            st = load_storage_state(storage_state_path)
            cookies = cookies_for_domains(st, ["cian.ru", "api.cian.ru"])
            self.session.cookies.update(dict(build_requests_cookie_jar(cookies)))

    def search_offers(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(SEARCH_OFFERS_URL, json=payload, timeout=self.timeout_s)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception as e:
            ct = resp.headers.get("content-type", "")
            snippet = (resp.text or "")[:500]
            raise RuntimeError(
                f"Cian API returned non-JSON response. status={resp.status_code} content-type={ct} snippet={snippet!r}"
            ) from e

    def search_offers_parsed(self, payload: Dict[str, Any]) -> List[CianOffer]:
        data = self.search_offers(payload)
        offers_raw = _extract_offers(data)
        out: List[CianOffer] = []
        for o in offers_raw:
            offer_id = o.get("id") or o.get("offerId") or o.get("internalId")
            if isinstance(offer_id, str) and offer_id.isdigit():
                offer_id = int(offer_id)
            if not isinstance(offer_id, int):
                continue
            out.append(
                CianOffer(
                    offer_id=offer_id,
                    cian_url=_guess_cian_url(o),
                    photos=_extract_photos(o),
                    seller_first_name=_extract_seller_first_name(o),
                    offer_description=_extract_description(o),
                    raw=o,
                )
            )
        return out


def with_paging(payload: Dict[str, Any], *, page: int, page_size: int) -> Dict[str, Any]:
    """
    Best-effort: inject paging controls into payload.
    Cian payloads often look like {"jsonQuery": {...}}.
    We add jsonQuery.page / jsonQuery.pageSize as term values.
    """
    p = json.loads(json.dumps(payload))  # deep copy via json
    jq = p.get("jsonQuery")
    if isinstance(jq, dict):
        jq.setdefault("page", {"type": "term", "value": page})
        jq.setdefault("pageSize", {"type": "term", "value": page_size})
        # If keys exist, override their values
        if isinstance(jq.get("page"), dict):
            jq["page"]["type"] = "term"
            jq["page"]["value"] = page
        if isinstance(jq.get("pageSize"), dict):
            jq["pageSize"]["type"] = "term"
            jq["pageSize"]["value"] = page_size
    return p


