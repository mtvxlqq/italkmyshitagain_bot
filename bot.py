"""Бот канала italkmyshitagain: оформление и публикация постов, отложка, подписчики, статистика.

Запуск: python bot.py. Где что лежит — в README, раздел «Устройство».
"""

import asyncio
import html
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.types import BotCommand

import handlers
from app import App
from config import Config, load_config
from db import Database
from files import FileCache
from publishing import scheduler_loop
from render import set_brand
from stats import daily_stats_loop

log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="queue", description="Отложенные посты"),
    BotCommand(command="stats", description="Статистика за сегодня"),
    BotCommand(command="help", description="Как сделать пост"),
    BotCommand(command="cancel", description="Отменить ввод"),
]


def create_app(cfg: Config, bot: Bot) -> App:
    return App(bot=bot, cfg=cfg, db=Database(cfg.db_path), files=FileCache(bot))


def create_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher(app=app)  # app получает каждый обработчик
    dp.include_routers(*handlers.routers())
    return dp


async def check_channel(app: App) -> None:
    """Проверяет, что бот — админ канала с правом публикации."""
    try:
        chat = await app.bot.get_chat(app.cfg.channel)
        app.channel_id, app.channel_username = chat.id, chat.username
        me = await app.bot.get_me()
        member = await app.bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        msg = f"Не могу получить доступ к каналу {app.cfg.channel}: {e}. Добавь бота в админы канала."
        log.error(msg)
        await app.notify_admins(f"⚠️ {html.escape(msg)}")
        return

    # шапка карточек: имя и аватарка канала (если не выйдет — останутся из макета)
    try:
        avatar = (await app.bot.download(chat.photo.big_file_id)).read() if chat.photo else None
        set_brand(chat.username, avatar)
    except Exception:
        log.exception("Не удалось взять аватарку канала")

    if member.status != ChatMemberStatus.ADMINISTRATOR or not getattr(member, "can_post_messages", False):
        msg = "Бот не админ канала или у него нет права публиковать сообщения."
        log.error(msg)
        await app.notify_admins(f"⚠️ {msg}")
    else:
        log.info("Канал: %s (%s)", chat.title, chat.id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = create_app(cfg, bot)
    dp = create_dispatcher(app)
    tasks: list[asyncio.Task] = []
    try:
        await check_channel(app)
        await bot.set_my_commands(COMMANDS)
        tasks = [asyncio.create_task(scheduler_loop(app)), asyncio.create_task(daily_stats_loop(app))]
        # chat_member приходит только если явно его запросить
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        app.files.close()


if __name__ == "__main__":
    asyncio.run(main())
