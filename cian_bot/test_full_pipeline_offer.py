from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests
from playwright.async_api import async_playwright

from cian_bot.cian_api import CianApi, with_paging
from cian_bot.config import load_core_settings
from cian_bot.console import safe_print
from cian_bot.n8n_create_photo import N8NCreatePhotoClient
from cian_bot.n8n_scoring import N8NPhotoScoringClient
from cian_bot.photo_score_validation import validate_photo_score
from cian_bot.photo_type import is_target_room_type
from cian_bot.state_store import StateStore


def _offer_id(url: str) -> int:
    m = re.search(r"/sale/flat/(\d+)/?", url)
    if not m:
        raise ValueError("Cannot extract offer_id from URL")
    return int(m.group(1))


async def _scrape_photo_urls(page, offer_url: str) -> list[str]:
    await page.goto(offer_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    # Grab all image URLs from DOM that look like Cian CDN photos.
    urls = await page.evaluate(
        """() => Array.from(document.images||[])
          .map(i => i.currentSrc || i.src)
          .filter(Boolean)
          .filter(u => u.includes('images.cdn-cian.ru/images/'))
        """
    )
    # Dedup keep order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _broad_payload(region_id: int = 4897) -> dict:
    return {
        "jsonQuery": {
            "_type": "flatsale",
            "engine_version": {"type": "term", "value": 2},
            "region": {"type": "terms", "value": [region_id]},
        }
    }


def _find_offer_via_api(offer_id: int, *, max_pages: int = 200) -> Optional[dict]:
    """
    Best-effort: scan search-offers pages using broad payload until we find the offer_id.
    Returns dict like {cian_url, photos, seller_first_name, offer_description}.
    """
    core = load_core_settings()
    api = CianApi(storage_state_path=core.storage_state_path)
    payload = _broad_payload()
    for page in range(1, max_pages + 1):
        paged = with_paging(payload, page=page, page_size=50)
        offers = api.search_offers_parsed(paged)
        if not offers:
            break
        for o in offers:
            if o.offer_id == offer_id:
                return {
                    "cian_url": o.cian_url,
                    "photos": o.photos,
                    "seller_first_name": o.seller_first_name,
                    "offer_description": o.offer_description,
                }
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Full test pipeline for a single offer: score -> create_photo -> send message+photo")
    ap.add_argument("--offer-url", required=True)
    ap.add_argument("--threshold", type=int, default=60)
    ap.add_argument("--delay-ms", type=int, default=300)
    ap.add_argument("--headless-scrape", action="store_true", help="Use headless for fallback scraping")
    args = ap.parse_args()

    offer_url = args.offer_url
    offer_id = _offer_id(offer_url)

    core = load_core_settings()
    store = StateStore(core.state_db_path)

    st = store.get(offer_id)
    if not st:
        safe_print(f"Offer {offer_id} not in DB; trying API backfill...")
        found = _find_offer_via_api(offer_id)
        if found:
            store.upsert_discovered(
                offer_id,
                found.get("cian_url") or offer_url,
                found.get("photos") or [],
                seller_first_name=found.get("seller_first_name"),
                offer_description=found.get("offer_description"),
            )
            st = store.get(offer_id)

    if not st or not st.photos:
        safe_print(f"Offer {offer_id}: no photos in DB. Fallback scrape from page via Playwright...")

        async def scrape_and_store():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=args.headless_scrape)
                ctx = await browser.new_context()
                page = await ctx.new_page()
                photos = await _scrape_photo_urls(page, offer_url)
                await ctx.close()
                await browser.close()
            store.upsert_discovered(offer_id, offer_url, photos)

        asyncio.run(scrape_and_store())
        st = store.get(offer_id)

    if not st or not st.photos:
        raise RuntimeError(f"Offer {offer_id}: still no photos. Cannot proceed.")

    # Step A: scoring until first interior* <= threshold
    scorer = N8NPhotoScoringClient()
    chosen = None  # (photo_url, score, photo_type)
    for idx, photo_url in enumerate(st.photos):
        if args.delay_ms > 0:
            time.sleep(args.delay_ms / 1000.0)
        res = scorer.score(offer_id=offer_id, idx=idx, photo_url=photo_url)
        res = validate_photo_score(res)
        ptype = str(res.get("photo_type") or "").strip().lower()
        score = int(res["improvement_score_0to100"])
        store.upsert_photo_analysis(
            offer_id=offer_id,
            photo_url=photo_url,
            idx=idx,
            category=ptype or None,
            score=score,
            raw_json=str(res),
        )
        safe_print(f"[score] idx={idx} type={ptype} score={score} url={photo_url}")
        if is_target_room_type(ptype) and score <= args.threshold:
            chosen = (photo_url, score, ptype)
            store.set_selected_photo(offer_id=offer_id, photo_url=photo_url, score=score, category=ptype)
            break

    if not chosen:
        raise RuntimeError(f"Offer {offer_id}: no interior* <= {args.threshold} found.")

    chosen_url, chosen_score, chosen_type = chosen
    safe_print("\n=== SELECTED PHOTO ===")
    safe_print(f"photo_type={chosen_type} score={chosen_score} url={chosen_url}")

    # Step B: create_photo webhook (send base64)
    img = requests.get(chosen_url, headers={"user-agent": "Mozilla/5.0"}, timeout=60)
    img.raise_for_status()
    mime = (img.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    b64 = base64.b64encode(img.content).decode("ascii")

    creator = N8NCreatePhotoClient()
    create_payload = {
        "offer_id": offer_id,
        "offer_url": offer_url,
        "seller_first_name": st.seller_first_name,
        "offer_description": st.offer_description,
        "selected_photo": {
            "photo_url": chosen_url,
            "photo_type": chosen_type,
            "improvement_score_0to100": chosen_score,
            "image_mime": mime,
            "image_base64": b64,
        },
    }
    resp = creator.create(create_payload)
    gdrive_id = resp.get("google_drive_file_id")
    msg_text = resp.get("message_text")
    safe_print("\n=== CREATE_PHOTO OUTPUT ===")
    safe_print(f"google_drive_file_id={gdrive_id}")
    safe_print(f"google_drive_view_url={resp.get('google_drive_view_url')}")
    safe_print(f"message_text={msg_text}")

    if not isinstance(gdrive_id, str) or not gdrive_id.strip():
        raise RuntimeError("create_photo did not return google_drive_file_id")
    if not isinstance(msg_text, str) or not msg_text.strip():
        raise RuntimeError("create_photo did not return message_text")

    # Step C: run Playwright sender using generated message + drive file id
    msg_file = Path(f"message_generated_{offer_id}.txt").resolve()
    msg_file.write_text(msg_text, encoding="utf-8")

    os.environ["CIAN_AD_URL"] = offer_url
    os.environ["CIAN_MESSAGE_FILE"] = str(msg_file)
    os.environ["GOOGLE_DRIVE_FILE_ID"] = gdrive_id.strip()
    os.environ.setdefault("CAPTCHA_PAUSE", "true")
    os.environ.setdefault("HEADLESS", "false")

    safe_print("\n=== SENDING VIA PLAYWRIGHT ===")
    safe_print(f"CIAN_AD_URL={offer_url}")
    safe_print(f"CIAN_MESSAGE_FILE={msg_file}")
    safe_print(f"GOOGLE_DRIVE_FILE_ID={gdrive_id}")

    import cian_bot.run as run_mod

    asyncio.run(run_mod.main())
    store.close()


if __name__ == "__main__":
    main()


