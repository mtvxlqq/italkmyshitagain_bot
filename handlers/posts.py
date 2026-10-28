"""Фото и видео с подписью → черновик поста: превью по шаблону, смена оформления, карусель."""

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import DOWNLOAD_LIMIT, App
from caption import build_caption, caption_too_long_for_message, parse_caption
from db import Post
from drafts import DEFAULT_MODE, draft_or_alert, draft_text, make_preview
from handlers.batch import Batcher
from handlers.filters import MEDIA, admin_router
from keyboards import PostCB, draft_kb
from render import MODES, clean_title, looks_like_quote

router = admin_router("posts")

ALBUM_WAIT = 1.5  # сек: части альбома приходят отдельными сообщениями, ждём все


def _source(message: Message) -> tuple[str, str, int]:
    """Тип ("photo"/"video"), file_id и размер исходного файла."""
    if message.photo:
        p = message.photo[-1]
        return "photo", p.file_id, p.file_size or 0
    if message.video:
        return "video", message.video.file_id, message.video.file_size or 0
    doc = message.document
    kind = "video" if (doc.mime_type or "").startswith("video/") else "photo"
    return kind, doc.file_id, doc.file_size or 0


async def process_post(app: App, messages: list[Message]) -> None:
    first = messages[0]
    caption_msg = next((m for m in messages if m.caption), None)
    if not caption_msg:
        await first.answer("Добавь подпись: первая строка — заголовок, дальше — текст поста.")
        return

    parsed = parse_caption(caption_msg.caption, caption_msg.caption_entities)
    if not clean_title(parsed.title):
        await first.answer("Не нашёл заголовок — первая строка подписи должна содержать текст.")
        return
    if caption_too_long_for_message(parsed, app.cfg.channel_link_text):
        await first.answer("Текст слишком длинный для Telegram (больше 4096 символов).")
        return

    sources = [_source(m) for m in messages]
    if any(size > DOWNLOAD_LIMIT for _, _, size in sources):
        await first.answer(
            "Файл больше 20 МБ — Telegram не даёт ботам скачивать такие. Сожми видео и пришли снова."
        )
        return

    style = {
        "mode": "quote" if parsed.blockquote or looks_like_quote(parsed.title) else DEFAULT_MODE,
        "accent": parsed.accent,
        "sources": [{"kind": k, "file_id": f} for k, f, _ in sources],
        "carousel": False,
    }
    caption, split = build_caption(parsed, app.cfg.channel_link_text, app.cfg.channel_url)
    media = await make_preview(app, first.chat.id, style, parsed.title, caption, split)
    if media is None:
        return
    post_id = app.db.add_draft(media, caption, split, parsed.title, style)
    await first.answer(draft_text(post_id, style, split), reply_markup=draft_kb(post_id, style))


_albums = Batcher(ALBUM_WAIT, process_post, "Не получилось оформить альбом")


@router.message(MEDIA)
async def on_media(message: Message, app: App, state: FSMContext) -> None:
    await state.clear()
    if message.media_group_id:  # части альбома приходят отдельными апдейтами — собираем
        _albums.add(app, message.media_group_id, message)
    else:
        await process_post(app, [message])


@router.callback_query(PostCB.filter(F.action == "mode"))
async def cb_mode(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    if not (post := await draft_or_alert(query, app, callback_data.id)):
        return
    if not post.style:
        await query.answer("Пост сделан до появления режимов — пришли его заново.", show_alert=True)
        return
    mode = callback_data.arg
    if post.style["mode"] == mode:
        await query.answer("Уже в этом оформлении")
        return
    await query.answer()
    # убираем кнопки, чтобы не нажать дважды, пока рендерится
    await query.message.edit_text(f"⏳ Пост #{post.id}: меняю оформление на {MODES[mode]}…")
    await _restyle(query, app, post, dict(post.style, mode=mode), f"оформление сменилось на {MODES[mode]}")


@router.callback_query(PostCB.filter(F.action == "carousel"))
async def cb_carousel(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    if not (post := await draft_or_alert(query, app, callback_data.id)):
        return
    if not post.style or len(post.style["sources"]) < 2:
        await query.answer("Карусель — для альбома из нескольких файлов.", show_alert=True)
        return
    on = not post.carousel
    await query.answer()
    await query.message.edit_text(f"⏳ Пост #{post.id}: {'собираю карусель' if on else 'убираю карусель'}…")
    await _restyle(query, app, post, dict(post.style, carousel=on),
                   "карусель включена" if on else "карусель выключена")


async def _restyle(query: CallbackQuery, app: App, post: Post, style: dict, done: str) -> None:
    """Перерисовывает черновик в новом оформлении и присылает новое превью с кнопками."""
    media = await make_preview(app, query.message.chat.id, style, post.title, post.caption_html, post.split_text)
    if media is None:
        await query.message.edit_text(draft_text(post.id, post.style, post.split_text),
                                      reply_markup=draft_kb(post.id, post.style))
        return
    app.db.update_media(post.id, media, style)
    await query.message.edit_text(f"Пост #{post.id}: {done}, новое превью ниже.")
    await query.message.answer(draft_text(post.id, style, post.split_text), reply_markup=draft_kb(post.id, style))
