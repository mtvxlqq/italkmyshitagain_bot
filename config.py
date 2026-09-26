import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная {name} (см. .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    channel: str  # @username или числовой id канала
    channel_link_text: str
    channel_url: str
    tz: ZoneInfo
    stats_time: time
    subscribers_threshold: int
    db_path: Path


def load_config() -> Config:
    channel = _required("CHANNEL")
    username = channel.lstrip("@")
    hh, mm = os.getenv("STATS_TIME", "21:00").split(":")
    db_path = Path(__file__).parent / os.getenv("DB_PATH", "bot.db")
    return Config(
        bot_token=_required("BOT_TOKEN"),
        admin_ids=frozenset(int(x) for x in _required("ADMIN_IDS").replace(" ", "").split(",") if x),
        channel=channel,
        channel_link_text=os.getenv("CHANNEL_LINK_TEXT", username),
        channel_url=os.getenv("CHANNEL_URL", f"https://t.me/{username}"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        stats_time=time(int(hh), int(mm)),
        subscribers_threshold=int(os.getenv("SUBSCRIBERS_THRESHOLD", "100")),
        db_path=db_path,
    )
