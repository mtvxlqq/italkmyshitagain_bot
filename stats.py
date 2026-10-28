"""Статистика канала: подписчики за день, опубликованные посты, очередь."""

import asyncio
import logging
from datetime import datetime, timedelta

from app import App

log = logging.getLogger(__name__)


async def build_stats(app: App, day_start: datetime, day_end: datetime) -> tuple[str, int]:
    count = await app.bot.get_chat_member_count(app.channel)
    events = app.db.member_events_between(day_start, day_end)
    joined, left = events.get("join", 0), events.get("leave", 0)

    prev = app.db.daily_count((day_start - timedelta(days=1)).strftime("%Y-%m-%d"))
    delta = count - prev if prev is not None else joined - left
    sign = "+" if delta >= 0 else ""

    posts = app.db.published_between(day_start, day_end)
    queued = len(app.db.scheduled_posts())
    text = (
        f"📊 <b>Статистика канала за {day_start.strftime('%d.%m.%Y')}</b>\n\n"
        f"👥 Подписчиков: <b>{count}</b> ({sign}{delta} за день)\n"
        f"➕ Подписались: {joined}\n"
        f"➖ Отписались: {left}\n"
        f"📝 Опубликовано постов: {posts}\n"
        f"⏰ В очереди: {queued}"
    )
    return text, count


def today_bounds(app: App) -> tuple[datetime, datetime]:
    now = app.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


async def daily_stats_loop(app: App) -> None:
    while True:
        now = app.now()
        run_at = now.replace(hour=app.cfg.stats_time.hour, minute=app.cfg.stats_time.minute, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        await asyncio.sleep((run_at - now).total_seconds())
        try:
            start, end = today_bounds(app)
            text, count = await build_stats(app, start, end)
            app.db.save_daily_count(start.strftime("%Y-%m-%d"), count)
            if count >= app.cfg.subscribers_threshold:
                await app.notify_admins(text)
        except Exception:
            log.exception("Ошибка ежедневной статистики")
        await asyncio.sleep(60)  # чтобы не сработать дважды в ту же минуту
