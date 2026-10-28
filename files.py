"""Файлы из Telegram на диске: скачанное один раз не качаем снова.

Смена оформления, карусель и музыка перерисовывают пост из исходников —
с кэшем это не требует новой загрузки. Старые файлы удаляются, когда кэш
перерастает лимит; сам кэш живёт во временной папке до остановки бота.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot

log = logging.getLogger(__name__)

CACHE_LIMIT = 300 * 1024 * 1024  # с запасом больше самого большого поста: 10 видео по 20 МБ
DOWNLOAD_TIMEOUT = 120  # сек: видео до 20 МБ по медленной сети


class FileCache:
    def __init__(self, bot: Bot, limit: int = CACHE_LIMIT):
        self.bot, self.limit = bot, limit
        self.root = Path(tempfile.mkdtemp(prefix="italkmyshitagain-files-"))
        self._downloads: dict[str, asyncio.Task[Path]] = {}

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def path(self, file_id: str) -> Path:
        """Локальная копия файла; одновременные запросы одного файла качают его один раз."""
        path = self.root / hashlib.sha1(file_id.encode()).hexdigest()
        if path.exists():
            os.utime(path)  # недавно нужный файл удалится последним
            return path
        task = self._downloads.get(file_id)
        if task is None:
            task = self._downloads[file_id] = asyncio.create_task(self._download(file_id, path))
            task.add_done_callback(lambda _: self._downloads.pop(file_id, None))
        # отмена того, кто ждёт, не обрывает загрузку — файл пригодится следующему
        return await asyncio.shield(task)

    async def read(self, file_id: str) -> bytes:
        return (await self.path(file_id)).read_bytes()

    async def paths(self, file_ids: list[str]) -> list[Path]:
        """Несколько файлов разом — качаются параллельно."""
        return list(await asyncio.gather(*map(self.path, file_ids)))

    async def _download(self, file_id: str, path: Path) -> Path:
        part = path.with_suffix(".part")
        try:
            await self.bot.download(file_id, destination=part, timeout=DOWNLOAD_TIMEOUT)
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)
        self._trim()
        return path

    def _trim(self) -> None:
        files = [(p.stat(), p) for p in self.root.iterdir() if p.suffix != ".part"]
        total = sum(st.st_size for st, _ in files)
        for st, p in sorted(files, key=lambda f: f[0].st_mtime):
            if total <= self.limit:
                break
            p.unlink(missing_ok=True)
            total -= st.st_size
            log.info("Кэш файлов: удалил %s (%d КБ)", p.name, st.st_size // 1024)
