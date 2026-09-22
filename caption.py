"""Разбор подписи: первая строка — заголовок, остальное — текст поста с сохранением форматирования."""

import html
from dataclasses import dataclass

from aiogram.types import MessageEntity
from aiogram.utils.text_decorations import html_decoration

# Лимит подписи к фото в Telegram (в UTF-16 символах)
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

_SPACES = {c.encode("utf-16-le") for c in " \n\r\t"}
_NEWLINE = "\n".encode("utf-16-le")


@dataclass
class ParsedPost:
    title: str       # заголовок простым текстом — для картинки
    text_html: str   # весь текст поста: заголовок жирным первой строкой + тело как было
    text_len: int    # длина текста без разметки, в UTF-16


def _u16(text: str) -> bytes:
    return text.encode("utf-16-le")


def _from_u16(data: bytes) -> str:
    return data.decode("utf-16-le")


def _slice_html(raw: bytes, entities: list[MessageEntity], start: int, end: int, skip: set[str] = frozenset()) -> str:
    """HTML куска текста [start, end) в UTF-16 со сдвинутыми сущностями."""
    shifted = []
    for e in entities:
        s, t = max(e.offset, start), min(e.offset + e.length, end)
        if t > s and e.type not in skip:
            shifted.append(e.model_copy(update={"offset": s - start, "length": t - s}))
    return html_decoration.unparse(_from_u16(raw[start * 2 : end * 2]), shifted)


def parse_caption(text: str, entities: list[MessageEntity] | None) -> ParsedPost:
    """Делит подпись на заголовок (первая строка) и тело.

    Смещения сущностей Telegram считаются в UTF-16, поэтому режем
    текст в той же кодировке и сдвигаем сущности на отрезанную часть.
    """
    entities = entities or []
    raw = _u16(text)
    n = len(raw) // 2
    units = [raw[i * 2 : i * 2 + 2] for i in range(n)]

    # пропускаем пустые строки в начале
    title_start = 0
    while title_start < n and units[title_start] in _SPACES:
        title_start += 1
    title_end = title_start
    while title_end < n and units[title_end] != _NEWLINE:
        title_end += 1
    nl = title_end
    while title_end > title_start and units[title_end - 1] in _SPACES:
        title_end -= 1

    body_start = nl
    while body_start < n and units[body_start] in _SPACES:
        body_start += 1
    body_end = n
    while body_end > body_start and units[body_end - 1] in _SPACES:
        body_end -= 1

    title = _from_u16(raw[title_start * 2 : title_end * 2])
    # жирный заголовка уже задаём сами — свой bold внутри не нужен
    text_html = f"<b>{_slice_html(raw, entities, title_start, title_end, skip={'bold'})}</b>"
    text_len = title_end - title_start
    if body_end > body_start:
        # сохраняем, сколько переносов строки было между заголовком и текстом
        sep = "\n" * max(1, units[nl:body_start].count(_NEWLINE))
        text_html += sep + _slice_html(raw, entities, body_start, body_end)
        text_len += len(sep) + body_end - body_start
    return ParsedPost(title=title, text_html=text_html, text_len=text_len)


def _link_len(link_text: str) -> int:
    return 2 + len(_u16(link_text)) // 2  # "\n\n" + ссылка


def build_caption(post: ParsedPost, link_text: str, link_url: str) -> tuple[str, bool]:
    """Возвращает HTML подписи и флаг «текст не влезает в подпись к фото»."""
    link = f'<b><a href="{html.escape(link_url, quote=True)}">{html.escape(link_text)}</a></b>'
    caption = f"{post.text_html}\n\n{link}"
    return caption, post.text_len + _link_len(link_text) > CAPTION_LIMIT


def caption_too_long_for_message(post: ParsedPost, link_text: str) -> bool:
    return post.text_len + _link_len(link_text) > TEXT_LIMIT
