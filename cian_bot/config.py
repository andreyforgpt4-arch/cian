from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


def _parse_bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    v = v.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default


@dataclass(frozen=True)
class CoreSettings:
    storage_state_path: Path
    headless: bool
    slow_mo_ms: int
    debug_pause: bool
    state_db_path: Path


@dataclass(frozen=True)
class Settings(CoreSettings):
    cian_ad_url: str
    cian_message: str
    google_drive_file_id: str

    dialogs_url: str = "https://novosibirsk.cian.ru/dialogs/"


def load_core_settings() -> CoreSettings:
    load_dotenv(override=False)

    storage_state_path = Path(os.getenv("STORAGE_STATE_PATH", "auth_state.json")).resolve()
    headless = _parse_bool(os.getenv("HEADLESS"), default=False)
    slow_mo_ms = int(os.getenv("SLOW_MO_MS", "0") or "0")
    debug_pause = _parse_bool(os.getenv("DEBUG_PAUSE"), default=False)
    state_db_path = Path(os.getenv("STATE_DB_PATH", "state.db")).resolve()

    return CoreSettings(
        storage_state_path=storage_state_path,
        headless=headless,
        slow_mo_ms=slow_mo_ms,
        debug_pause=debug_pause,
        state_db_path=state_db_path,
    )


def load_settings() -> Settings:
    core = load_core_settings()

    cian_ad_url = os.getenv("CIAN_AD_URL", "").strip()
    cian_message_file = os.getenv("CIAN_MESSAGE_FILE", "").strip()
    if cian_message_file:
        msg_path = Path(cian_message_file).expanduser().resolve()
        cian_message = msg_path.read_text(encoding="utf-8").strip()
    else:
        cian_message = os.getenv("CIAN_MESSAGE", "").strip()
    google_drive_file_id = os.getenv("GOOGLE_DRIVE_FILE_ID", "").strip()

    if not cian_ad_url:
        raise RuntimeError("Missing env CIAN_AD_URL")
    if not cian_message:
        raise RuntimeError("Missing env CIAN_MESSAGE (or CIAN_MESSAGE_FILE)")
    if not google_drive_file_id:
        raise RuntimeError("Missing env GOOGLE_DRIVE_FILE_ID")

    return Settings(
        cian_ad_url=cian_ad_url,
        cian_message=cian_message,
        google_drive_file_id=google_drive_file_id,
        storage_state_path=core.storage_state_path,
        headless=core.headless,
        slow_mo_ms=core.slow_mo_ms,
        debug_pause=core.debug_pause,
        state_db_path=core.state_db_path,
    )


