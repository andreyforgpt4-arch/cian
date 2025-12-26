from __future__ import annotations

import argparse
import json
from pathlib import Path

from cian_bot.cian_api import CianApi, with_paging
from cian_bot.config import load_core_settings
from cian_bot.state_store import StateStore


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill a single offer fields (seller_first_name/description) via Cian API")
    ap.add_argument("--payload", required=True, help="Path to payload.json used for search-offers")
    ap.add_argument("--offer-id", type=int, required=True)
    ap.add_argument("--max-pages", type=int, default=50)
    args = ap.parse_args()

    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    core = load_core_settings()
    api = CianApi(storage_state_path=core.storage_state_path)
    store = StateStore(core.state_db_path)

    for page in range(1, args.max_pages + 1):
        paged = with_paging(payload, page=page, page_size=50)
        offers = api.search_offers_parsed(paged)
        if not offers:
            break
        for o in offers:
            if o.offer_id == args.offer_id:
                store.upsert_discovered(
                    o.offer_id,
                    o.cian_url,
                    o.photos,
                    seller_first_name=o.seller_first_name,
                    offer_description=o.offer_description,
                    raw=o.raw,
                )
                print(f"Backfilled offer_id={o.offer_id} seller_first_name={o.seller_first_name!r} desc_len={len(o.offer_description or '')}")
                store.close()
                return

    store.close()
    raise SystemExit(f"Offer {args.offer_id} not found within {args.max_pages} pages for given payload.")


if __name__ == "__main__":
    main()


