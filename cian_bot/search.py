from __future__ import annotations

import argparse
import json
from pathlib import Path

from cian_bot.cian_api import CianApi, with_paging
from cian_bot.config import load_core_settings
from cian_bot.state_store import StateStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Cian search-offers API runner")
    parser.add_argument("--payload", required=True, help="Path to JSON payload for search-offers API")
    parser.add_argument("--out", default="search_result.json", help="Where to save raw API response")
    parser.add_argument("--out-offers", default="offers_extracted.json", help="Where to save extracted offers list")
    parser.add_argument("--limit", type=int, default=50, help="How many offers to collect (best-effort)")
    parser.add_argument("--max-pages", type=int, default=10, help="Max pages to try when collecting offers")
    args = parser.parse_args()

    payload_path = Path(args.payload).resolve()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    core = load_core_settings()
    api = CianApi(storage_state_path=core.storage_state_path)
    store = StateStore(core.state_db_path)

    collected = []
    last_raw = None
    for page in range(1, args.max_pages + 1):
        paged = with_paging(payload, page=page, page_size=min(50, args.limit))
        last_raw = api.search_offers(paged)
        offers = api.search_offers_parsed(paged)
        for o in offers:
            store.upsert_discovered(
                o.offer_id,
                o.cian_url,
                o.photos,
                seller_first_name=o.seller_first_name,
                offer_description=o.offer_description,
                raw=o.raw,
            )
            collected.append(o)
            if len(collected) >= args.limit:
                break
        if len(collected) >= args.limit:
            break
        if not offers:
            break

    if last_raw is not None:
        Path(args.out).write_text(json.dumps(last_raw, ensure_ascii=False, indent=2), encoding="utf-8")

    extracted = [
        {
            "offer_id": o.offer_id,
            "cian_url": o.cian_url,
            "photos": o.photos,
        }
        for o in collected
    ]
    Path(args.out_offers).write_text(json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Offers extracted: {len(extracted)}")
    if extracted:
        print("First offer:", extracted[0])
    print(f"State DB: {core.state_db_path}")
    store.close()


if __name__ == "__main__":
    main()


