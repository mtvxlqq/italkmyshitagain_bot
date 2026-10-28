"""Что делать с черновиком: опубликовать сейчас, отложить, снять с очереди, удалить."""

import logging
from datetime import datetime

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import NO_PREVIEW, App
from drafts import draft_or_alert
from handlers.filters import admin_router
from keyboards import PostCB, draft_kb, quick_kb, scheduled_kb
from publishing import publish_claimed
from timeparse import parse_when, quick_time

log = logging.getLogger(__name__)

router = admin_router("publish")

SCHEDULE_PROMPT = (
    "Когда опубликовать пост #{id}? Напиши время, например:\n"
    "• <code>18:30</code> — сегодня (или завтра, если уже прошло)\n"
    "• <code>завтра 10:00</code>\n"
    "• <code>25.09 19:00</code> или <code>25.09.2026 19:00</code>\n"
    "• <code>+2ч</code>, <code>+30м</code>\n\n"
    "Часовой пояс: {tz}. Или выбери вариант ниже."
)


class ScheduleForm(StatesGroup):
    waiting_time = State()


async def _replace_text(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Меняет текст сообщения с кнопками (у фото из /queue — подпись)."""
    msg = query.message
    if msg.photo or msg.video:
        await msg.edit_caption(caption=text, reply_markup=markup)
    else:
        await msg.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(PostCB.filter(F.action.in_({"publish", "now"})))
async def cb_publish(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    allowed = ("draft",) if callback_data.action == "publish" else ("scheduled",)
    try:
        url = await publish_claimed(app, callback_data.id, allowed)
    except Exception as e:
        log.exception("Ошибка публикации")
        await query.answer(f"Ошибка: {e}"[:190], show_alert=True)
        return
    if url is None:
        await query.answer("Этот пост уже опубликован или отменён.", show_alert=True)
        return
    await query.answer("Опубликовано!")
    await _replace_text(query, f"✅ Пост #{callback_data.id} опубликован: {url}")


@router.callback_query(PostCB.filter(F.action == "schedule"))
async def cb_schedule(query: CallbackQuery, callback_data: PostCB, app: App, state: FSMContext) -> None:
    if not (post := await draft_or_alert(query, app, callback_data.id)):
        return
    await state.set_state(ScheduleForm.waiting_time)
    await state.update_data(post_id=post.id)
    await query.answer()
    await query.message.answer(
        SCHEDULE_PROMPT.format(id=post.id, tz=app.cfg.tz.key),
        reply_markup=quick_kb(post.id),
    )


def _schedule(app: App, post_id: int, when: datetime) -> str:
    """Ставит черновик в очередь. Пустая строка — получилось, иначе — почему нет."""
    post = app.db.get_post(post_id)
    if not post or post.status != "draft":
        return "Этот пост уже обработан."
    if when <= app.now():
        return "Это время уже прошло — укажи время в будущем."
    app.db.schedule(post_id, when)
    return ""


@router.callback_query(PostCB.filter(F.action == "quick"))
async def cb_quick(query: CallbackQuery, callback_data: PostCB, app: App, state: FSMContext) -> None:
    when = quick_time(callback_data.arg, app.now())
    if error := _schedule(app, callback_data.id, when):
        await query.answer(error, show_alert=True)
        return
    await state.clear()
    await query.answer("Запланировано")
    await query.message.edit_text(
        f"⏰ Пост #{callback_data.id} будет опубликован {app.fmt(when)}",
        reply_markup=scheduled_kb(callback_data.id),
    )


@router.message(ScheduleForm.waiting_time, F.text)
async def on_schedule_time(message: Message, app: App, state: FSMContext) -> None:
    when = parse_when(message.text, app.now())
    if when is None:
        await message.answer("Не понял время. Пример: <code>18:30</code>, <code>завтра 10:00</code>, "
                             "<code>25.09 19:00</code>, <code>+2ч</code>. Или /cancel.")
        return
    post_id = (await state.get_data())["post_id"]
    if error := _schedule(app, post_id, when):
        await message.answer(error)
        if "прошло" not in error:
            await state.clear()
        return
    await state.clear()
    await message.answer(
        f"⏰ Пост #{post_id} будет опубликован {app.fmt(when)}",
        reply_markup=scheduled_kb(post_id),
    )


@router.callback_query(PostCB.filter(F.action == "unschedule"))
async def cb_unschedule(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    post = app.db.get_post(callback_data.id)
    if not post or post.status != "scheduled":
        await query.answer("Этот пост уже не в очереди.", show_alert=True)
        return
    app.db.set_status(post.id, "draft")
    await query.answer("Снят с публикации")
    await _replace_text(query, f"Пост #{post.id} снят с публикации — снова черновик.",
                        draft_kb(post.id, post.style))


@router.callback_query(PostCB.filter(F.action == "discard"))
async def cb_discard(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    if not (post := await draft_or_alert(query, app, callback_data.id)):
        return
    app.db.set_status(post.id, "cancelled")
    await query.answer("Удалено")
    await _replace_text(query, f"🗑 Черновик #{post.id} удалён.")
