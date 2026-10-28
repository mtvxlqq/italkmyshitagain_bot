"""Отправка постов (альбомом или статьёй-каруселью), публикация в канал и очередь отложенных.

Медиа для отправки — словари: type ("photo" / "video"), file (file_id или файл для загрузки),
у видео — width, height, duration. После отправки от них остаются file_id: по ним пост
публикуется без повторной загрузки.
"""

import asyncio
import html
import logging
import re

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile, FSInputFile, InputMediaPhoto, InputMediaVideo, InputRichMessage, InputRichMessageMedia,
    Message,
)

from app import NO_PREVIEW, App
from db import Post
from video import VideoResult

log = logging.getLogger(__name__)

SCHEDULER_INTERVAL = 15  # секунд между проверками очереди
_VIDEO_FIELDS = ("width", "height", "duration")


def photo_item(image: bytes, name: str) -> dict:
    return {"type": "photo", "file": BufferedInputFile(image, f"{name}.jpg")}


def video_item(video: VideoResult) -> dict:
    return {"type": "video", "file": FSInputFile(video.path),
            "width": video.width, "height": video.height, "duration": video.duration}


def stored_items(media: list[dict]) -> list[dict]:
    """Уже загруженные в Telegram медиа — отправляем по file_id."""
    return [dict(m, file=m["file_id"]) for m in media]


def _video_kwargs(item: dict) -> dict:
    return {k: item[k] for k in _VIDEO_FIELDS if item.get(k)}


def _stored(sent, item: dict) -> dict:
    """Что сохранить о медиа из превью, чтобы потом опубликовать его без повторной загрузки.

    sent — отправленное сообщение или слайд статьи: у обоих есть photo/video.
    """
    if item["type"] == "video":
        return {"type": "video", "file_id": sent.video.file_id, **{k: item[k] for k in _VIDEO_FIELDS}}
    return {"type": "photo", "file_id": sent.photo[-1].file_id}


async def send_media(bot: Bot, chat_id: int | str, items: list[dict], caption: str | None) -> list[Message]:
    """Отправляет одно медиа или альбом с подписью у первого."""
    if len(items) == 1:
        item = items[0]
        if item["type"] == "video":
            return [await bot.send_video(
                chat_id, item["file"], caption=caption, supports_streaming=True, **_video_kwargs(item)
            )]
        return [await bot.send_photo(chat_id, item["file"], caption=caption)]

    group = []
    for i, item in enumerate(items):
        extra = {"caption": caption} if i == 0 and caption else {}
        if item["type"] == "video":
            group.append(InputMediaVideo(media=item["file"], supports_streaming=True, **_video_kwargs(item), **extra))
        else:
            group.append(InputMediaPhoto(media=item["file"], **extra))
    return await bot.send_media_group(chat_id, group)


_RICH_BLOCKS = re.compile(r"(<blockquote[^>]*>.*?</blockquote>|<pre>.*?</pre>)", re.S)


def rich_text(caption: str) -> str:
    """Подпись в разметке Telegram → HTML статьи: цитаты и код — отдельными блоками,
    остальное — абзацами, переносы строк — <br>."""
    out = []
    for i, part in enumerate(_RICH_BLOCKS.split(caption)):
        if i % 2:  # цитата или код
            out.append(part if part.startswith("<pre>") else part.replace("\n", "<br>"))
        elif part := part.strip("\n"):
            out.append("<p>" + part.replace("\n", "<br>") + "</p>")
    return "".join(out)


async def send_carousel(bot: Bot, chat_id: int | str, items: list[dict], caption: str) -> Message:
    """Статья Telegram: слайды листаются свайпом прямо в ленте, под ними — текст поста."""
    media, slides = [], []
    for i, item in enumerate(items):
        if item["type"] == "video":
            media.append(InputRichMessageMedia(id=f"m{i}", media=InputMediaVideo(
                media=item["file"], supports_streaming=True, **_video_kwargs(item))))
            slides.append(f'<video src="tg://video?id=m{i}"></video>')
        else:
            media.append(InputRichMessageMedia(id=f"m{i}", media=InputMediaPhoto(media=item["file"])))
            slides.append(f'<img src="tg://photo?id=m{i}"/>')
    html_ = f"<tg-slideshow>{''.join(slides)}</tg-slideshow>{rich_text(caption)}"
    return await bot.send_rich_message(chat_id, InputRichMessage(html=html_, media=media))


async def send_post(bot: Bot, chat_id: int | str, items: list[dict], caption: str, split: bool,
                    carousel: bool = False) -> tuple[int, list[dict]]:
    """Отправляет пост: альбом с подписью (длинный текст — следом отдельным сообщением)
    или статью-карусель. Возвращает id первого сообщения и медиа для повторной отправки."""
    if carousel:
        sent = await send_carousel(bot, chat_id, items, caption)
        show = next(b for b in sent.rich_message.blocks if b.type == "slideshow")
        return sent.message_id, [_stored(b, item) for b, item in zip(show.blocks, items)]
    album = await send_media(bot, chat_id, items, None if split else caption)
    if split:
        await bot.send_message(chat_id, caption, link_preview_options=NO_PREVIEW)
    return album[0].message_id, [_stored(m, item) for m, item in zip(album, items)]


async def publish(app: App, post: Post) -> str:
    """Отправляет пост в канал, возвращает ссылку на него (или название канала)."""
    message_id, _ = await send_post(app.bot, app.channel, stored_items(post.media), post.caption_html,
                                    post.split_text, post.carousel)
    app.db.set_status(post.id, "published")
    if app.channel_username:
        return f"https://t.me/{app.channel_username}/{message_id}"
    return app.cfg.channel


async def publish_claimed(app: App, post_id: int, allowed: tuple[str, ...]) -> str | None:
    """Публикует пост, если он ещё в одном из статусов allowed. None — уже обработан кем-то другим."""
    if not app.db.claim_for_publishing(post_id, allowed):
        return None
    post = app.db.get_post(post_id)
    try:
        return await publish(app, post)
    except Exception:
        # возвращаем прежний статус, чтобы можно было повторить
        app.db.set_status(post_id, allowed[0])
        raise


async def scheduler_loop(app: App) -> None:
    while True:
        try:
            for post in app.db.due_posts():
                if not app.db.claim_for_publishing(post.id, ("scheduled",)):
                    continue
                try:
                    url = await publish(app, post)
                    await app.notify_admins(
                        f"✅ Отложенный пост #{post.id} «{html.escape(post.title)}» опубликован: {url}",
                        link_preview_options=NO_PREVIEW,
                    )
                except Exception as e:
                    log.exception("Ошибка публикации поста %s", post.id)
                    app.db.set_status(post.id, "failed")
                    await app.notify_admins(
                        f"⚠️ Не удалось опубликовать отложенный пост #{post.id}: <code>{html.escape(str(e))}</code>"
                    )
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(SCHEDULER_INTERVAL)
