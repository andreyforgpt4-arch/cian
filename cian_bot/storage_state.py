from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple


def load_storage_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cookies_for_domains(storage_state: dict, domains: List[str]) -> List[dict]:
    """
    Returns cookies whose domain matches one of provided domains (suffix match).
    """
    out: List[dict] = []
    for c in storage_state.get("cookies", []) or []:
        domain = (c.get("domain") or "").lstrip(".")
        if any(domain == d.lstrip(".") or domain.endswith("." + d.lstrip(".")) for d in domains):
            out.append(c)
    return out


def build_cookie_header(cookies: List[dict]) -> str:
    parts: List[str] = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def build_requests_cookie_jar(cookies: List[dict]) -> List[Tuple[str, str]]:
    """
    Simple name/value list that can be fed into requests.Session().cookies.update(dict(...)).
    """
    out: List[Tuple[str, str]] = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            out.append((name, value))
    return out


