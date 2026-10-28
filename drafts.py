"""Черновики: исходники из Telegram → медиа по шаблону → превью админу.

Оформление черновика (style) — словарь: mode — режим из render.MODES, accent — красные куски
заголовка, sources — исходные файлы [{"kind": "photo" | "video", "file_id"}], carousel — статья
со слайдами. По нему пост перерисовывается при смене оформления и для музыки.
"""

import asyncio
import html
import tempfile
from functools import partial
from pathlib import Path

from aiogram.types import CallbackQuery

from app import App
from db import Post
from publishing import photo_item, send_post, video_item
from render import MODES, RELEASES_MAX, Card, card_layout, image_size, make_card, render_card, render_plain
from video import VideoError, render_video

DEFAULT_MODE = "news"


class PostError(Exception):
    """Ошибка оформления, которую можно показать как есть."""


def cover_indexes(sources: list[dict]) -> list[int]:
    """Обложки релизов — первые фото альбома."""
    return [i for i, s in enumerate(sources) if s["kind"] == "photo"][:RELEASES_MAX]


def announce_cover(sources: list[dict]) -> dict | None:
    """В анонсе второе фото — обложка: встаёт в карточку вместо «?»."""
    return sources[1] if len(sources) > 1 and sources[1]["kind"] == "photo" else None


async def render_media(app: App, chat_id: int, style: dict, card: Card, workdir: Path) -> list[dict]:
    """Оформляет исходники для send_post; готовые видео кладёт в workdir.

    Первый файл — карточка шаблона, остальные — без оверлея. В релизах первые фото
    (до трёх) склеиваются в одну карточку с обложками, в анонсе второе фото — обложка в карточке.
    В карусели слайды без оверлея — ровно размера первого, чтобы при листании кадр не прыгал.
    """
    mode, sources, carousel = style["mode"], style["sources"], style.get("carousel", False)
    action = "upload_video" if any(s["kind"] == "video" for s in sources) else "upload_photo"
    items: list[dict] = []

    def slide_size() -> tuple[int, int] | None:
        """Размер слайда без оверлея: в карусели — как у первого, иначе — по кадру."""
        if not carousel or not items:
            return None
        first = items[0]
        if first["type"] == "video":
            return first["width"], first["height"]
        return image_size(first["file"].data)

    async with app.render_lock:
        await app.bot.send_chat_action(chat_id, action)
        rest = list(range(len(sources)))
        covers = cover_indexes(sources) if mode == "releases" else []
        if mode == "releases" and not covers:
            raise PostError("Для релизов нужны фото обложек (до трёх).")
        paths = await app.files.paths([s["file_id"] for s in sources])  # все разом, параллельно
        if covers:
            items.append(photo_item(await asyncio.to_thread(render_card, mode, card, [paths[i] for i in covers]),
                                    "0"))
            rest = [i for i in rest if i not in covers]
        cover = None
        if mode == "announce" and announce_cover(sources):
            cover = paths[1]
            rest.remove(1)

        for n, i in enumerate(rest):
            await app.bot.send_chat_action(chat_id, action)
            templated = n == 0 and mode not in ("releases", "plain")
            if sources[i]["kind"] == "photo":
                if templated:
                    image = await asyncio.to_thread(render_card, mode, card, [paths[i]] + ([cover] if cover else []))
                else:
                    image = await asyncio.to_thread(render_plain, paths[i], slide_size())
                items.append(photo_item(image, str(len(items))))
            else:
                video = await render_video(
                    paths[i], workdir / f"{len(items)}.mp4",
                    partial(card_layout, mode, card, cover=cover) if templated else None,
                    None if templated else slide_size(),
                )
                items.append(video_item(video))
    return items


async def make_preview(app: App, chat_id: int, style: dict, title: str, caption: str,
                       split: bool) -> list[dict] | None:
    """Оформляет пост, присылает превью и возвращает медиа для черновика.

    None — не получилось; причину уже написали в чат.
    """
    sources = style["sources"]
    has_video = any(s["kind"] == "video" for s in sources)
    status = None
    if has_video or len(sources) > 1:
        status = "⏳ Оформляю…" + (" Видео обрабатывается дольше фото." if has_video else "")
    with tempfile.TemporaryDirectory() as tmp:  # готовые видео лежат здесь, пока не уйдут в превью
        try:
            async with app.progress(chat_id, status):
                card = make_card(title, style["accent"], style["mode"])
                items = await render_media(app, chat_id, style, card, Path(tmp))
        except (PostError, VideoError) as e:
            await app.bot.send_message(chat_id, f"⚠️ {html.escape(str(e))}")
            return None
        _, media = await send_post(app.bot, chat_id, items, caption, split, style.get("carousel", False))
    return media


async def first_image(app: App, post: Post) -> bytes:
    """Первая картинка поста в полном качестве: перерисовываем из исходников, а не берём сжатое превью."""
    style = post.style
    if not style:  # пост до появления режимов — берём готовую картинку
        return await app.files.read(post.media[0]["file_id"])
    mode, sources = style["mode"], style["sources"]
    if mode == "releases":
        picked = [sources[i] for i in cover_indexes(sources)]
    elif mode == "announce" and (cover := announce_cover(sources)):
        picked = [sources[0], cover]
    else:
        picked = sources[:1]
    photos = await app.files.paths([s["file_id"] for s in picked])
    if mode == "plain":
        return await asyncio.to_thread(render_plain, photos[0])
    return await asyncio.to_thread(render_card, mode, make_card(post.title, style["accent"], mode), photos)


def draft_text(post_id: int, style: dict | None, split: bool, extra: str = "") -> str:
    head = f"👆 Превью поста #{post_id}"
    if style:
        carousel = ", карусель" if style.get("carousel") else ""
        head += f" — {MODES[style['mode']]}{carousel}{extra}. Другое оформление — кнопками ниже"
    text = head + ". Что делаем?"
    if split and not (style and style.get("carousel")):  # в статью влезает до 32768 символов
        text += ("\n\n⚠️ Текст длиннее 1024 символов — в подпись не влезет, "
                 "поэтому опубликую его отдельным сообщением сразу под постом.")
    return text


async def draft_or_alert(query: CallbackQuery, app: App, post_id: int) -> Post | None:
    """Черновик по нажатой кнопке; уже опубликован, отложен или удалён — говорит об этом и даёт None."""
    post = app.db.get_post(post_id)
    if not post or post.status != "draft":
        await query.answer("Этот пост уже обработан.", show_alert=True)
        return None
    return post
