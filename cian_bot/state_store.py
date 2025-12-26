from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OfferState:
    offer_id: int
    cian_url: Optional[str]
    photos_json: str
    status: str
    updated_at: str
    last_error: Optional[str]
    selected_photo_url: Optional[str] = None
    selected_photo_score: Optional[int] = None
    selected_photo_category: Optional[str] = None
    seller_first_name: Optional[str] = None
    offer_description: Optional[str] = None

    @property
    def photos(self) -> List[str]:
        try:
            v = json.loads(self.photos_json or "[]")
            return v if isinstance(v, list) else []
        except Exception:
            return []


class StateStore:
    """
    SQLite store to avoid re-processing the same offers across runs.
    Status lifecycle (suggested):
      discovered -> scored -> messaged -> photo_sent
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS offers (
              offer_id INTEGER PRIMARY KEY,
              cian_url TEXT,
              photos_json TEXT,
              status TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_error TEXT,
              selected_photo_url TEXT,
              selected_photo_score INTEGER,
              selected_photo_category TEXT,
              seller_first_name TEXT,
              offer_description TEXT
            )
            """
        )
        # Backward-compatible ALTERs if table existed earlier.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(offers)").fetchall()}
        if "selected_photo_url" not in cols:
            self.conn.execute("ALTER TABLE offers ADD COLUMN selected_photo_url TEXT")
        if "selected_photo_score" not in cols:
            self.conn.execute("ALTER TABLE offers ADD COLUMN selected_photo_score INTEGER")
        if "selected_photo_category" not in cols:
            self.conn.execute("ALTER TABLE offers ADD COLUMN selected_photo_category TEXT")
        if "seller_first_name" not in cols:
            self.conn.execute("ALTER TABLE offers ADD COLUMN seller_first_name TEXT")
        if "offer_description" not in cols:
            self.conn.execute("ALTER TABLE offers ADD COLUMN offer_description TEXT")

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_analyses (
              offer_id INTEGER NOT NULL,
              photo_url TEXT NOT NULL,
              idx INTEGER NOT NULL,
              category TEXT,
              score INTEGER,
              raw_json TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY (offer_id, photo_url)
            )
            """
        )
        self.conn.commit()

    def upsert_discovered(
        self,
        offer_id: int,
        cian_url: Optional[str],
        photos: List[str],
        *,
        seller_first_name: Optional[str] = None,
        offer_description: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        # raw currently unused; keep signature for future expansion
        photos_json = json.dumps(photos, ensure_ascii=False)
        now = _now_iso()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO offers (offer_id, cian_url, photos_json, status, updated_at, last_error)
            VALUES (?, ?, ?, 'discovered', ?, NULL)
            ON CONFLICT(offer_id) DO UPDATE SET
              cian_url = COALESCE(excluded.cian_url, offers.cian_url),
              photos_json = CASE
                WHEN excluded.photos_json IS NOT NULL AND excluded.photos_json != '[]' THEN excluded.photos_json
                ELSE offers.photos_json
              END,
              seller_first_name = COALESCE(?, offers.seller_first_name),
              offer_description = COALESCE(?, offers.offer_description),
              updated_at = excluded.updated_at
              -- CRITICAL: NEVER update status in upsert_discovered - it should only be set via set_status()
              -- Status is NOT updated here to preserve existing status
            """,
            (offer_id, cian_url, photos_json, now, seller_first_name, offer_description),
        )
        self.conn.commit()

    def set_status(self, offer_id: int, status: str, last_error: Optional[str] = None) -> None:
        now = _now_iso()
        self.conn.execute(
            "UPDATE offers SET status=?, updated_at=?, last_error=? WHERE offer_id=?",
            (status, now, last_error, offer_id),
        )
        self.conn.commit()

    def upsert_photo_analysis(
        self,
        *,
        offer_id: int,
        photo_url: str,
        idx: int,
        category: Optional[str],
        score: Optional[int],
        raw_json: str,
    ) -> None:
        now = _now_iso()
        self.conn.execute(
            """
            INSERT INTO photo_analyses (offer_id, photo_url, idx, category, score, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_id, photo_url) DO UPDATE SET
              idx=excluded.idx,
              category=excluded.category,
              score=excluded.score,
              raw_json=excluded.raw_json
            """,
            (offer_id, photo_url, idx, category, score, raw_json, now),
        )
        self.conn.commit()

    def set_selected_photo(self, *, offer_id: int, photo_url: str, score: int, category: str) -> None:
        now = _now_iso()
        self.conn.execute(
            """
            UPDATE offers
            SET selected_photo_url=?, selected_photo_score=?, selected_photo_category=?, updated_at=?
            WHERE offer_id=?
            """,
            (photo_url, score, category, now, offer_id),
        )
        self.conn.commit()

    def get(self, offer_id: int) -> Optional[OfferState]:
        row = self.conn.execute(
            """
            SELECT offer_id, cian_url, photos_json, status, updated_at, last_error,
                   selected_photo_url, selected_photo_score, selected_photo_category,
                   seller_first_name, offer_description
            FROM offers WHERE offer_id=?
            """,
            (offer_id,),
        ).fetchone()
        if not row:
            return None
        return OfferState(*row)

    def list_by_status(self, statuses: Iterable[str], limit: int = 50) -> List[OfferState]:
        st = list(statuses)
        if not st:
            return []
        placeholders = ",".join("?" for _ in st)
        rows = self.conn.execute(
            f"""
            SELECT offer_id, cian_url, photos_json, status, updated_at, last_error,
                   selected_photo_url, selected_photo_score, selected_photo_category,
                   seller_first_name, offer_description
            FROM offers
            WHERE status IN ({placeholders})
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (*st, limit),
        ).fetchall()
        return [OfferState(*r) for r in rows]

    def get_worst_analyzed_photo(self, offer_id: int) -> Optional[dict]:
        """
        Returns dict with keys: photo_url, score, category, idx for the worst (min score) analyzed photo.
        """
        row = self.conn.execute(
            """
            SELECT photo_url, score, category, idx
            FROM photo_analyses
            WHERE offer_id=? AND score IS NOT NULL
            ORDER BY score ASC, idx ASC
            LIMIT 1
            """,
            (offer_id,),
        ).fetchone()
        if not row:
            return None
        return {"photo_url": row[0], "score": row[1], "category": row[2], "idx": row[3]}

    def get_worst_interior_photo(self, offer_id: int) -> Optional[dict]:
        """
        Returns dict with keys: photo_url, score, category, idx for the worst analyzed INTERIOR photo.
        """
        row = self.conn.execute(
            """
            SELECT photo_url, score, category, idx
            FROM photo_analyses
            WHERE offer_id=? AND score IS NOT NULL AND lower(category) LIKE 'interior%'
            ORDER BY score ASC, idx ASC
            LIMIT 1
            """,
            (offer_id,),
        ).fetchone()
        if not row:
            return None
        return {"photo_url": row[0], "score": row[1], "category": row[2], "idx": row[3]}

    def get_first_interior_below(self, offer_id: int, threshold: int) -> Optional[dict]:
        """
        Returns first (by idx ASC) analyzed INTERIOR photo with score <= threshold.
        """
        row = self.conn.execute(
            """
            SELECT photo_url, score, category, idx
            FROM photo_analyses
            WHERE offer_id=? AND score IS NOT NULL AND lower(category) LIKE 'interior%' AND score <= ?
            ORDER BY idx ASC
            LIMIT 1
            """,
            (offer_id, threshold),
        ).fetchone()
        if not row:
            return None
        return {"photo_url": row[0], "score": row[1], "category": row[2], "idx": row[3]}


