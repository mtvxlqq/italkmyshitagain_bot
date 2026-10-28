"""Сообщения, отправленные разом, но пришедшие по одному: части альбома, пачка пересланных."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable, Hashable

from aiogram.types import Message

from app import App

log = logging.getLogger(__name__)


class Batcher:
    """Копит сообщения по ключу и отдаёт пачкой (по порядку), когда новые перестают приходить wait секунд.

    Ошибку обработки пачки пишет в лог и в чат: «⚠️ {error}: …».
    """

    def __init__(self, wait: float, handler: Callable[[App, list[Message]], Awaitable[None]], error: str):
        self.wait, self.handler, self.error = wait, handler, error
        self._messages: dict[Hashable, list[Message]] = {}
        self._timers: dict[Hashable, asyncio.Task] = {}
        self._running: set[asyncio.Task] = set()  # держим ссылки, пока пачка обрабатывается

    def add(self, app: App, key: Hashable, message: Message) -> None:
        self._messages.setdefault(key, []).append(message)
        if timer := self._timers.get(key):
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._flush(app, key))

    async def _flush(self, app: App, key: Hashable) -> None:
        await asyncio.sleep(self.wait)
        # дальше пачка закрыта: новое сообщение с тем же ключом начнёт следующую
        task = self._timers.pop(key)
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        messages = sorted(self._messages.pop(key), key=lambda m: m.message_id)
        try:
            await self.handler(app, messages)
        except Exception as e:
            log.exception(self.error)
            await messages[0].answer(f"⚠️ {self.error}: <code>{html.escape(str(e))}</code>")
