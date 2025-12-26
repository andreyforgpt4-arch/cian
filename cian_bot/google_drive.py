from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

import requests


def _sanitize_filename(file_id: str, filename: str, content_type: str) -> str:
    """
    Ensure filename is safe for Windows consoles/filesystems and tooling.
    If filename contains non-ascii characters, fall back to gdrive_<id>.<ext>.
    """
    # Keep a best-effort extension.
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[1]
    if not ext and content_type.startswith("image/"):
        ext_guess = content_type.split("/", 1)[1].strip()
        if ext_guess == "jpeg":
            ext_guess = "jpg"
        ext = "." + ext_guess

    try:
        filename.encode("ascii")
        # Also restrict to a conservative set of characters.
        if re.fullmatch(r"[A-Za-z0-9._-]+", filename or ""):
            return filename
    except Exception:
        pass

    return f"gdrive_{file_id}{ext or ''}"


def _get_confirm_token_from_cookies(resp: requests.Response) -> Optional[str]:
    # For large files Google Drive may require a confirmation token.
    for k, v in resp.cookies.items():
        if k.startswith("download_warning"):
            return v
    return None


def download_public_file(file_id: str, out_dir: Path) -> Path:
    """
    Download a publicly accessible Google Drive file by ID.
    Returns local file path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    base_url = "https://drive.google.com/uc"
    params = {"export": "download", "id": file_id}

    resp = session.get(base_url, params=params, stream=True, timeout=60)
    resp.raise_for_status()

    token = _get_confirm_token_from_cookies(resp)
    if token:
        resp.close()
        params["confirm"] = token
        resp = session.get(base_url, params=params, stream=True, timeout=60)
        resp.raise_for_status()

    filename = f"gdrive_{file_id}"
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        filename = m.group(1)

    ct = (resp.headers.get("Content-Type") or "").lower()
    # If file isn't shared publicly, Google Drive often returns an HTML page.
    if "text/html" in ct:
        try:
            snippet = resp.content[:800].decode("utf-8", errors="replace")
        except Exception:
            snippet = "<cannot decode html>"
        raise RuntimeError(
            f"Google Drive file is not publicly downloadable (got text/html). "
            f"Make sure it's shared 'Anyone with the link can view'. Snippet={snippet!r}"
        )
    filename = _sanitize_filename(file_id=file_id, filename=filename, content_type=ct)

    out_path = out_dir / filename
    with out_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)

    return out_path


