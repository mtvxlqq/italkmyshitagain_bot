"""Трек после поста → видео из карточки с отрывком этого трека."""

import html
import tempfile
from pathlib import Path

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app import DOWNLOAD_LIMIT, App
from drafts import draft_text, first_image
from handlers.filters import AUDIO, admin_router
from keyboards import draft_kb
from publishing import send_post, stored_items, video_item
from timeparse import MUSIC_DEFAULT_LEN, parse_timecode
from video import VideoError, photo_to_music_video

router = admin_router("music")


class MusicForm(StatesGroup):
    waiting_timecode = State()


@router.message(AUDIO)
async def on_audio(message: Message, app: App, state: FSMContext) -> None:
    post = app.db.latest_draft()
    if not post:
        await message.answer("Сначала пришли пост с фото — музыку я наложу на его последний черновик.")
        return
    style = post.style
    photo_first = (
        (style["mode"] == "releases" or style["sources"][0]["kind"] == "photo") if style
        else post.media[0]["type"] == "photo"
    )
    if not photo_first:
        await message.answer(f"Пост #{post.id} начинается с видео — музыку накладываю только на фото.")
        return
    audio = message.audio or message.document
    if (audio.file_size or 0) > DOWNLOAD_LIMIT:
        await message.answer("Трек больше 20 МБ — Telegram не даёт ботам скачивать такие файлы.")
        return

    await state.set_state(MusicForm.waiting_timecode)
    await state.update_data(post_id=post.id, audio_id=audio.file_id)
    length = ""
    if message.audio and message.audio.duration:
        d = message.audio.duration
        length = f" (длина трека {d // 60}:{d % 60:02d})"
    await message.answer(
        f"🎵 Сделаю из поста #{post.id} «{html.escape(post.title)}» видео с этим треком{length}.\n\n"
        f"С какого момента? Пришли таймкод:\n"
        f"• <code>1:05</code> — {MUSIC_DEFAULT_LEN} секунд с 1:05\n"
        f"• <code>1:05-1:40</code> — конкретный отрывок\n\n"
        f"Или /cancel."
    )


@router.message(MusicForm.waiting_timecode, F.text)
async def on_timecode(message: Message, app: App, state: FSMContext) -> None:
    tc = parse_timecode(message.text)
    if tc is None:
        await message.answer("Не понял таймкод. Пример: <code>1:05</code> или <code>1:05-1:40</code>. Или /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    post = app.db.get_post(data["post_id"])
    if not post or post.status != "draft":
        await message.answer("Этот пост уже опубликован или удалён.")
        return

    with tempfile.TemporaryDirectory() as tmp:  # видео лежит здесь, пока не уйдёт в превью
        try:
            async with app.progress(message.chat.id, "⏳ Делаю видео с музыкой…"), app.render_lock:
                await app.bot.send_chat_action(message.chat.id, "upload_video")
                image = await first_image(app, post)
                audio = await app.files.path(data["audio_id"])
                video = await photo_to_music_video(image, audio, Path(tmp) / "post.mp4", *tc)
        except VideoError as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            return
        items = [video_item(video), *stored_items(post.media[1:])]
        _, media = await send_post(app.bot, message.chat.id, items, post.caption_html, post.split_text,
                                   post.carousel)
    app.db.update_media(post.id, [media[0], *post.media[1:]], post.style)
    await message.answer(
        draft_text(post.id, post.style, post.split_text, extra=" с музыкой")
        + "\n\nДругой отрывок — пришли трек ещё раз. Смена оформления уберёт музыку.",
        reply_markup=draft_kb(post.id, post.style),
    )
