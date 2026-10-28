"""Что какому обработчику: виды сообщений и доступ только для админов."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app import App

# фото/видео — пост, трек — музыка к посту
MEDIA = F.photo | F.video | (
    F.document & (F.document.mime_type.startswith("image/") | F.document.mime_type.startswith("video/"))
)
AUDIO = F.audio | (F.document & F.document.mime_type.startswith("audio/"))
# пересланное — в переписку, кроме треков и фото/видео из каналов: из них, как раньше, пост
FORWARD = F.forward_origin & ~AUDIO & (~MEDIA | F.forward_origin.type.in_({"user", "hidden_user"}))


def is_admin(event: Message | CallbackQuery, app: App) -> bool:
    return event.from_user is not None and event.from_user.id in app.cfg.admin_ids


def admin_router(name: str) -> Router:
    """Роутер для админов: личные сообщения и нажатия кнопок только от них."""
    router = Router(name=name)
    router.message.filter(F.chat.type == "private", is_admin)
    router.callback_query.filter(is_admin)
    return router
