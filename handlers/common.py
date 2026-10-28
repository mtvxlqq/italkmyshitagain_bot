"""Команды (/start, /help, /cancel, /stats, /queue) и ответ на непредвиденные ошибки."""

import html
import logging
from contextlib import suppress

from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import ErrorEvent, Message

from app import App
from handlers.filters import admin_router
from keyboards import scheduled_kb
from stats import build_stats, today_bounds

log = logging.getLogger(__name__)

router = admin_router("common")

HELP = (
    "<b>Как сделать пост</b>\n"
    "Пришли фото или видео (или альбом до 10 штук) с подписью:\n"
    "• <b>первая строка</b> — заголовок: попадёт на карточку и останется "
    "первой строкой поста жирным;\n"
    "• всё остальное — текст поста, оставлю как есть (с форматированием).\n\n"
    "<b>Оформление</b> — кнопками под превью:\n"
    "• 📰 <b>Новость</b> — фото в рамке, заголовок снизу (по умолчанию);\n"
    "• 📣 <b>Анонс</b> — фото артиста слева, справа карточка: второе фото альбома "
    "(обложка) или «?», если его нет; заголовок по центру;\n"
    "• 💿 <b>Релизы</b> — «Эти релизы вышли сегодня» и до трёх обложек из альбома;\n"
    "• 💬 <b>Цитата</b> — затемнённое чёрно-белое фото и текст в «ёлочках» "
    "(включается само, если первая строка в кавычках или оформлена цитатой);\n"
    "• 🖼 <b>Без оверлея</b> — только обрезка под формат.\n\n"
    "<b>Карусель</b> — для альбома из нескольких файлов: кнопка 🎠 под превью. Пост уйдёт "
    "статьёй Telegram: слайды листаются свайпом прямо в ленте, текст — под ними. Оверлей только "
    "на первом слайде, остальные — ровно того же размера. Включается вместе с любым оформлением.\n\n"
    "Слова заголовка, выделенные <i>курсивом</i> или <u>подчёркиванием</u>, будут красными. "
    "Без выделения красным станет конец заголовка. В анонсе и цитате хвост "
    "<code>— Имя</code> уйдёт в подпись под заголовком (артист / автор).\n\n"
    "Карточка — только первый файл, остальные файлы альбома идут без оформления. "
    "В конец текста добавлю жирную ссылку на канал. "
    "Дальше — опубликовать сразу или отложить.\n\n"
    "Видео — до 20 МБ (ограничение Telegram для ботов).\n\n"
    "<b>Музыка</b>\n"
    "Пришли трек после поста — спрошу таймкод и сделаю из карточки видео с этим отрывком.\n\n"
    "<b>Переписка</b>\n"
    "Перешли мне сообщения из чата — соберу их в картинку «Переписка»: ник, текст и время "
    "каждого сообщения. Длинная переписка займёт несколько картинок (до 10). "
    "Фото и видео из личных чатов тоже попадут в переписку — подписью «Фото»/«Видео», "
    "а пересланные из каналов останутся обычным постом.\n\n"
    "<b>Команды</b>\n"
    "/queue — отложенные посты\n"
    "/stats — статистика канала за сегодня\n"
    "/cancel — отменить ввод времени"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.")


@router.message(Command("stats"))
async def cmd_stats(message: Message, app: App) -> None:
    start, end = today_bounds(app)
    text, _ = await build_stats(app, start, end)
    await message.answer(text)


@router.message(Command("queue"))
async def cmd_queue(message: Message, app: App) -> None:
    posts = app.db.scheduled_posts()
    if not posts:
        await message.answer("Очередь пуста.")
        return
    await message.answer(f"Отложенных постов: {len(posts)}")
    for post in posts:
        first = post.media[0]
        album = f" (альбом, {len(post.media)} шт.)" if len(post.media) > 1 else ""
        text = f"#{post.id} «{html.escape(post.title)}»{album}\n⏰ {app.fmt(post.publish_at)}"
        send = message.answer_video if first["type"] == "video" else message.answer_photo
        await send(first["file_id"], caption=text, reply_markup=scheduled_kb(post.id))


@router.errors()
async def on_error(event: ErrorEvent, app: App) -> None:
    """Неперехваченная ошибка в обработчике: в лог и коротко — в чат, чтобы не гадать, почему бот молчит."""
    log.error("Ошибка при обработке апдейта %s", event.update.update_id, exc_info=event.exception)
    update = event.update
    message = update.message or (update.callback_query and update.callback_query.message)
    if message:
        with suppress(TelegramAPIError):
            await app.bot.send_message(
                message.chat.id, f"⚠️ Не получилось: <code>{html.escape(str(event.exception))}</code>"
            )
