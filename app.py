"""Общее для всего бота: App — бот, настройки, база и файлы, которые получает каждый обработчик."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from config import Config
from db import Database
from files import FileCache

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
DOWNLOAD_LIMIT = 20 * 1024 * 1024  # больше Bot API скачать не даёт


@dataclass
class App:
    bot: Bot
    cfg: Config
    db: Database
    files: FileCache
    channel_id: int | None = None
    channel_username: str | None = None
    # рендер видео тяжёлый — обрабатываем посты по одному
    render_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def channel(self) -> int | str:
        """Куда публиковать: id канала, а пока он не известен — как в настройках."""
        return self.channel_id or self.cfg.channel

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    def fmt(self, dt: datetime) -> str:
        return dt.astimezone(self.cfg.tz).strftime("%d.%m.%Y %H:%M")

    async def notify_admins(self, text: str, **kwargs) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, **kwargs)
            except Exception:
                log.exception("Не удалось отправить сообщение админу %s", admin_id)

    @asynccontextmanager
    async def progress(self, chat_id: int, text: str | None) -> AsyncIterator[None]:
        """«⏳ …» в чате, пока идёт долгая работа; text=None — без сообщения."""
        status = await self.bot.send_message(chat_id, text) if text else None
        try:
            yield
        finally:
            if status:
                with suppress(TelegramAPIError):
                    await status.delete()
