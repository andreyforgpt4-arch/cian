from __future__ import annotations

import argparse
import re

from cian_bot.config import load_core_settings
from cian_bot.console import safe_print
from cian_bot.n8n_create_photo import N8NCreatePhotoClient
from cian_bot.state_store import StateStore
import requests
import base64


def _offer_id(url: str) -> int:
    m = re.search(r"/sale/flat/(\d+)/?", url)
    if not m:
        raise ValueError("Cannot extract offer_id from URL")
    return int(m.group(1))


async def main_async(offer_url: str, threshold: int) -> None:
    offer_id = _offer_id(offer_url)
    core = load_core_settings()
    store = StateStore(core.state_db_path)

    first_bad = store.get_first_interior_below(offer_id, threshold)
    selected = first_bad or store.get_worst_interior_photo(offer_id)
    if not selected:
        raise RuntimeError(f"No interior photo analyses found for offer_id={offer_id}. Run scoring first.")

    st = store.get(offer_id)
    seller_first_name = st.seller_first_name if st else None
    offer_description = st.offer_description if st else None
    if not offer_description:
        raise RuntimeError(
            "Missing offer_description in state.db for this offer. "
            "Re-run `python -m cian_bot.search ...` after updating code to store this field."
        )

    payload = {
        "offer_id": offer_id,
        "offer_url": offer_url,
        "seller_first_name": seller_first_name,
        "offer_description": offer_description,
        "selected_photo": {
            "photo_url": selected["photo_url"],
            "photo_type": "interior",
            "improvement_score_0to100": selected["score"],
        },
    }

    # For create_photo webhook we send the selected photo as base64
    img = requests.get(selected["photo_url"], headers={"user-agent": "Mozilla/5.0"}, timeout=60)
    img.raise_for_status()
    payload["selected_photo"]["image_mime"] = (img.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    payload["selected_photo"]["image_base64"] = base64.b64encode(img.content).decode("ascii")

    client = N8NCreatePhotoClient()
    resp = client.create(payload)

    safe_print("\n=== SENT PAYLOAD ===")
    safe_print(payload)
    safe_print("\n=== WEBHOOK RESPONSE ===")
    safe_print(resp)

    store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Test create_photo webhook with a single offer")
    ap.add_argument("--offer-url", required=True)
    ap.add_argument("--threshold", type=int, default=60)
    args = ap.parse_args()

    import asyncio

    asyncio.run(main_async(args.offer_url, args.threshold))


if __name__ == "__main__":
    main()


