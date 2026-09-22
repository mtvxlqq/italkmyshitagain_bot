"""Хранилище: посты (черновики/отложенные/опубликованные), события подписки, снимки статистики."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_file_id TEXT NOT NULL,  -- первое медиа; полный список — в media
    media         TEXT,           -- JSON: [{"type": "photo"|"video", "file_id": ..., ...}]
    caption_html  TEXT NOT NULL,
    split_text    INTEGER NOT NULL DEFAULT 0,  -- текст длиннее лимита подписи: шлём отдельным сообщением
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',  -- draft | scheduled | published | cancelled | failed
    publish_at    TEXT,
    published_at  TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,  -- join | leave
    ts      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_counts (
    day   TEXT PRIMARY KEY,  -- YYYY-MM-DD в часовом поясе бота
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Post:
    id: int
    media: list[dict]
    caption_html: str
    split_text: bool
    title: str
    status: str
    publish_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Post":
        return cls(
            id=row["id"],
            media=json.loads(row["media"]) if row["media"] else [{"type": "photo", "file_id": row["photo_file_id"]}],
            caption_html=row["caption_html"],
            split_text=bool(row["split_text"]),
            title=row["title"],
            status=row["status"],
            publish_at=datetime.fromisoformat(row["publish_at"]) if row["publish_at"] else None,
        )


class Database:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # миграция баз, созданных до поддержки альбомов и видео
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "media" not in columns:
            self.conn.execute("ALTER TABLE posts ADD COLUMN media TEXT")
        self.conn.commit()

    # --- посты ---

    def add_draft(self, media: list[dict], caption_html: str, split_text: bool, title: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO posts (photo_file_id, media, caption_html, split_text, title, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (media[0]["file_id"], json.dumps(media), caption_html, int(split_text), title, utcnow().isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_post(self, post_id: int) -> Post | None:
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return Post.from_row(row) if row else None

    def latest_draft(self) -> Post | None:
        row = self.conn.execute("SELECT * FROM posts WHERE status = 'draft' ORDER BY id DESC LIMIT 1").fetchone()
        return Post.from_row(row) if row else None

    def update_media(self, post_id: int, media: list[dict]) -> None:
        self.conn.execute(
            "UPDATE posts SET media = ?, photo_file_id = ? WHERE id = ?",
            (json.dumps(media), media[0]["file_id"], post_id),
        )
        self.conn.commit()

    def schedule(self, post_id: int, when: datetime) -> None:
        self.conn.execute(
            "UPDATE posts SET status = 'scheduled', publish_at = ? WHERE id = ?",
            (when.astimezone(timezone.utc).isoformat(), post_id),
        )
        self.conn.commit()

    def set_status(self, post_id: int, status: str) -> None:
        published_at = utcnow().isoformat() if status == "published" else None
        self.conn.execute(
            "UPDATE posts SET status = ?, published_at = COALESCE(?, published_at) WHERE id = ?",
            (status, published_at, post_id),
        )
        self.conn.commit()

    def claim_for_publishing(self, post_id: int, allowed: tuple[str, ...]) -> bool:
        """Атомарно переводит пост в 'publishing', чтобы его не опубликовали дважды."""
        marks = ",".join("?" * len(allowed))
        cur = self.conn.execute(
            f"UPDATE posts SET status = 'publishing' WHERE id = ? AND status IN ({marks})",
            (post_id, *allowed),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def scheduled_posts(self) -> list[Post]:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE status = 'scheduled' ORDER BY publish_at"
        ).fetchall()
        return [Post.from_row(r) for r in rows]

    def due_posts(self) -> list[Post]:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE status = 'scheduled' AND publish_at <= ? ORDER BY publish_at",
            (utcnow().isoformat(),),
        ).fetchall()
        return [Post.from_row(r) for r in rows]

    def published_between(self, start: datetime, end: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status = 'published' AND published_at >= ? AND published_at < ?",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        ).fetchone()[0]

    # --- подписчики ---

    def add_member_event(self, user_id: int, kind: str) -> None:
        self.conn.execute(
            "INSERT INTO member_events (user_id, kind, ts) VALUES (?, ?, ?)",
            (user_id, kind, utcnow().isoformat()),
        )
        self.conn.commit()

    def member_events_between(self, start: datetime, end: datetime) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) FROM member_events WHERE ts >= ? AND ts < ? GROUP BY kind",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        ).fetchall()
        return {kind: n for kind, n in rows}

    def save_daily_count(self, day: str, count: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO daily_counts (day, count) VALUES (?, ?)", (day, count))
        self.conn.commit()

    def daily_count(self, day: str) -> int | None:
        row = self.conn.execute("SELECT count FROM daily_counts WHERE day = ?", (day,)).fetchone()
        return row[0] if row else None

    # --- настройки ---

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()
