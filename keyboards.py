"""Кнопки под сообщениями бота."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from render import MODES

CAROUSEL_LABEL = "🎠 Карусель"


class PostCB(CallbackData, prefix="post"):
    action: str  # publish | schedule | discard | unschedule | now | quick | mode | carousel
    id: int
    arg: str = ""


def _button(text: str, action: str, post_id: int, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=PostCB(action=action, id=post_id, arg=arg).pack())


def draft_kb(post_id: int, style: dict | None = None) -> InlineKeyboardMarkup:
    """Кнопки черновика; style — текущее оформление (None у постов до режимов — без переключателей)."""
    rows = []
    if style:
        modes = [_button(("✓ " if key == style["mode"] else "") + label, "mode", post_id, key)
                 for key, label in MODES.items()]
        rows += [modes[:3], modes[3:]]
        if len(style["sources"]) > 1:
            on = style.get("carousel", False)
            rows.append([_button(("✓ " if on else "") + CAROUSEL_LABEL, "carousel", post_id)])
    return InlineKeyboardMarkup(inline_keyboard=[
        *rows,
        [_button("🚀 Опубликовать", "publish", post_id), _button("⏰ Отложить", "schedule", post_id)],
        [_button("✖️ Удалить черновик", "discard", post_id)],
    ])


def quick_kb(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button("Через 1 час", "quick", post_id, "h1"), _button("Через 3 часа", "quick", post_id, "h3")],
        [_button("Завтра 10:00", "quick", post_id, "t10"), _button("Завтра 19:00", "quick", post_id, "t19")],
    ])


def scheduled_kb(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _button("🚀 Сейчас", "now", post_id), _button("❌ Отменить", "unschedule", post_id),
    ]])
