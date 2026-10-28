"""Пересланные сообщения → картинки «Переписка» и черновик поста из них."""

import asyncio
import logging

from aiogram.fsm.context import FSMContext
from aiogram.types import Message, MessageOriginChannel, MessageOriginChat, MessageOriginHiddenUser, MessageOriginUser

from app import App
from caption import channel_link
from drafts import draft_text
from handlers.batch import Batcher
from handlers.filters import FORWARD, admin_router
from keyboards import draft_kb
from publishing import photo_item, send_post
from render import CHAT_PAGES_MAX, ChatMessage, render_chat

log = logging.getLogger(__name__)

router = admin_router("chat")

FORWARD_WAIT = 2.0  # сек: пересланные разом сообщения тоже приходят по одному
# чем заменить вложение в пузыре (порядок важен: у GIF есть и document)
_ATTACHMENTS = [
    ("photo", "Фото"), ("video", "Видео"), ("animation", "GIF"), ("video_note", "Видеосообщение"),
    ("voice", "Голосовое сообщение"), ("sticker", "Стикер"), ("document", "Файл"),
    ("poll", "Опрос"), ("location", "Геопозиция"), ("contact", "Контакт"),
]


def _sender(message: Message) -> tuple[str, str]:
    """Имя автора пересланного сообщения и ключ, по которому у собеседника один цвет."""
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name, f"user{origin.sender_user.id}"
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name, f"hidden{origin.sender_user_name}"
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.title or "", f"chat{origin.sender_chat.id}"
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.title or "", f"chat{origin.chat.id}"
    return "", "unknown"


async def _avatar(app: App, message: Message) -> bytes | None:
    """Аватарка автора пересланного; нет её или скрыта настройками приватности — None."""
    origin = message.forward_origin
    try:
        if isinstance(origin, MessageOriginUser):
            photos = await app.bot.get_user_profile_photos(origin.sender_user.id, limit=1)
            file_id = photos.photos[0][0].file_id if photos.photos else None
        elif isinstance(origin, (MessageOriginChat, MessageOriginChannel)):
            chat = origin.sender_chat if isinstance(origin, MessageOriginChat) else origin.chat
            info = await app.bot.get_chat(chat.id)
            file_id = info.photo.small_file_id if info.photo else None
        else:
            return None
        return await app.files.read(file_id) if file_id else None
    except Exception:
        log.warning("Не получилось взять аватарку", exc_info=True)
        return None


async def process_chat(app: App, messages: list[Message]) -> None:
    first = messages[0]
    senders = [_sender(m) for m in messages]
    async with app.progress(first.chat.id, "⏳ Оформляю переписку…" if len(messages) > 5 else None):
        # по одной аватарке на собеседника, все разом
        firsts: dict[str, Message] = {}
        for m, (_, key) in zip(messages, senders):
            firsts.setdefault(key, m)
        avatars = dict(zip(firsts, await asyncio.gather(*(_avatar(app, m) for m in firsts.values()))))
        chat = []
        for m, (name, key) in zip(messages, senders):
            chat.append(ChatMessage(
                sender=name, key=key,
                time=m.forward_origin.date.astimezone(app.cfg.tz).strftime("%H:%M"),
                text=m.text or m.caption or "",
                note=next((label for attr, label in _ATTACHMENTS if getattr(m, attr, None)), ""),
                avatar=avatars[key],
            ))
        pages = await asyncio.to_thread(render_chat, chat)
    if not pages:
        await first.answer("В пересланных сообщениях нечего нарисовать: в них только символы, которых нет в шрифте.")
        return
    if len(pages) > CHAT_PAGES_MAX:
        await first.answer(
            f"Переписка вышла на {len(pages)} картинок, а в пост влезает {CHAT_PAGES_MAX}. "
            "Перешли меньше сообщений."
        )
        return

    caption = channel_link(app.cfg.channel_link_text, app.cfg.channel_url)
    items = [photo_item(page, str(i)) for i, page in enumerate(pages)]
    _, media = await send_post(app.bot, first.chat.id, items, caption, split=False)
    names = list(dict.fromkeys(m.sender for m in chat if m.sender))
    title = "Переписка" + (f": {', '.join(names[:3])}" if names else "")
    post_id = app.db.add_draft(media, caption, False, title)
    await first.answer(draft_text(post_id, None, False), reply_markup=draft_kb(post_id))


_forwards = Batcher(FORWARD_WAIT, process_chat, "Не получилось оформить переписку")


@router.message(FORWARD)
async def on_forward(message: Message, app: App, state: FSMContext) -> None:
    await state.clear()
    _forwards.add(app, message.chat.id, message)
