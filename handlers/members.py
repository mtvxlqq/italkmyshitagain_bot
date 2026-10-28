"""Подписчики канала: о каждом новом пишем, пока их меньше порога; все приходы и уходы — в статистику."""

import html

from aiogram import Router
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from app import App

router = Router(name="members")


def _user_link(event: ChatMemberUpdated) -> str:
    user = event.new_chat_member.user
    name = html.escape(user.full_name)
    link = f'<a href="tg://user?id={user.id}">{name}</a>'
    return f"{link} (@{user.username})" if user.username else link


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated, app: App) -> None:
    if event.chat.id != app.channel_id:
        return
    app.db.add_member_event(event.new_chat_member.user.id, "join")
    count = await app.bot.get_chat_member_count(event.chat.id)
    threshold = app.cfg.subscribers_threshold

    if count <= threshold:
        await app.notify_admins(f"🆕 Новый подписчик: {_user_link(event)}\n👥 Всего: {count}/{threshold}")
    if count >= threshold and not app.db.get_setting("threshold_reached"):
        app.db.set_setting("threshold_reached", "1")
        await app.notify_admins(
            f"🎉 В канале {count} подписчиков! Больше не пишу про каждого — "
            f"теперь каждый день в {app.cfg.stats_time.strftime('%H:%M')} присылаю статистику."
        )


@router.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_leave(event: ChatMemberUpdated, app: App) -> None:
    if event.chat.id == app.channel_id:
        app.db.add_member_event(event.new_chat_member.user.id, "leave")
