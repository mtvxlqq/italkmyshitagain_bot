"""Карточки постов по шаблонам канала: новость, анонс, релизы, цитата — и режим без оверлея.

Шаблон — это непрозрачный слой поверх фото: фон со сканлайнами, шапка канала, надписи.
На месте фото в слое «окно» (полная или частичная прозрачность), поэтому один и тот же
слой годится и для фото (склеиваем в PIL), и для видео (ffmpeg кладёт его поверх кадра).

Новый шаблон: функция, которая строит Layout, + ветка в card_layout + подпись в MODES.
"""

import io
import math
import re
from dataclasses import dataclass
from functools import cache

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .common import (
    ASSETS, BG, CARD, CARD_BORDER, DARK_RED, DIVIDER, GRAY, LIGHT, MAX_WIDE, PILL_BORDER, RED, SCANLINE, WHITE,
    ImageSource, canvas_size, even, fit_whole, open_image, rounded_mask, to_alpha, to_jpeg,
)
from .text import (
    TITLE_FONT, Font, cap, clean_title, draw_rich, draw_tracked, get_font, label_font, text_width, tracked_width,
)

AVATAR_DEFAULT = ASSETS / "avatar.png"

# режимы: ключ — подпись на кнопке
MODES = {
    "news": "📰 Новость",
    "announce": "📣 Анонс",
    "releases": "💿 Релизы",
    "quote": "💬 Цитата",
    "plain": "🖼 Без оверлея",
}
RELEASES_MAX = 3  # обложек на карточке релизов


# ---------- канал в шапке ----------

@dataclass
class Brand:
    name: str
    handle: str
    avatar: Image.Image


_brand = Brand("italkmyshitagain", "@italkmyshitagain", Image.open(AVATAR_DEFAULT).convert("RGBA"))


def set_brand(username: str | None = None, avatar: bytes | None = None) -> None:
    """Имя канала и аватарка для шапки (по умолчанию — вырезанные из макета)."""
    if username:
        _brand.name, _brand.handle = username, f"@{username}"
    if avatar:
        _brand.avatar = Image.open(io.BytesIO(avatar)).convert("RGBA")
    _avatar.cache_clear()


@cache
def _avatar(size: int) -> Image.Image:
    img = ImageOps.fit(_brand.avatar, (size, size), Image.LANCZOS)
    # круг рисуем крупнее и уменьшаем — так край сглажен
    mask = Image.new("L", (size * 4, size * 4))
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    alpha = np.asarray(img.getchannel("A"), dtype=np.float32) / 255
    alpha *= np.asarray(mask.resize((size, size), Image.LANCZOS), dtype=np.float32)
    img.putalpha(Image.fromarray(alpha.round().astype(np.uint8)))
    return img


# ---------- заголовок: слова, выделение, раскладка по строкам ----------

Word = tuple[str, bool]  # слово заглавными и выделено ли оно красным
Line = list[Word]

# заголовок-цитата: «текст» или «текст» — автор (кавычки любые)
_QUOTE_RE = re.compile(r'^\s*["«„“]([^"«»„“”]+)["»“”]\s*(?:[—–-]\s*(.+?))?\s*$')
# «текст — автор», «новый альбом на след неделе — артист»
_AUTHOR_RE = re.compile(r"^(.+?)\s+[—–]\s+([^—–]+?)\s*$")


def looks_like_quote(title: str) -> bool:
    return bool(_QUOTE_RE.match(title))


@dataclass
class Card:
    words: list[Word]
    author: str | None = None  # артист в анонсе, автор цитаты


def make_card(title: str, accent: list[tuple[int, int]], mode: str) -> Card:
    """Текст для карточки из первой строки подписи.

    accent — куски заголовка (индексы символов), выделенные в подписи курсивом или
    подчёркиванием: они будут красными. Если ничего не выделено, красным становится
    конец заголовка: в анонсе — вторая половина слов, в новости — последняя половина без среднего.
    В цитате и анонсе хвост «— имя» уходит в подпись под заголовком.
    """
    red = [False] * len(title)
    for s, e in accent:
        for i in range(max(s, 0), min(e, len(title))):
            red[i] = True

    start, end, author = 0, len(title), None
    if mode == "quote" and (m := _QUOTE_RE.match(title)):
        start, end, author = m.start(1), m.end(1), m[2]
    elif mode in ("quote", "announce") and (m := _AUTHOR_RE.match(title)):
        start, end, author = m.start(1), m.end(1), m[2]

    words: list[Word] = []
    for w in re.finditer(r"\S+", title[start:end]):
        text = clean_title(w.group())
        if text:
            words.append((text, any(red[start + w.start() : start + w.end()])))

    if not any(r for _, r in words) and len(words) > 1:
        tail = {"announce": math.ceil(len(words) / 2), "news": len(words) // 2}.get(mode, 0)
        words = [(w, i >= len(words) - tail) for i, (w, _) in enumerate(words)]

    author = clean_title(author) if author else None
    return Card(words, author or None)


def _line_width(line: Line, font: Font) -> float:
    return sum(text_width(w, font) for w, _ in line) + font.getlength(" ") * (len(line) - 1)


class _Measured:
    """Слова абзаца, измеренные одним кеглем: перебор переносов не мерит одно и то же заново.

    Ширина строки считается так же, как в _line_width, — до последнего бита.
    """

    def __init__(self, words: list[Word], font: Font):
        self.words = words
        self.widths = [text_width(w, font) for w, _ in words]
        self.space = font.getlength(" ")

    def width(self, i: int, j: int) -> float:
        """Ширина строки из слов words[i:j]."""
        return sum(self.widths[i:j]) + self.space * (j - i - 1)

    def lines(self, breaks: list[tuple[int, int]]) -> list[Line]:
        return [self.words[i:j] for i, j in breaks]


def _greedy(m: _Measured, max_w: float) -> list[Line]:
    breaks, start = [], 0
    for j in range(1, len(m.words)):
        if m.width(start, j + 1) > max_w:
            breaks.append((start, j))
            start = j
    return m.lines(breaks + [(start, len(m.words))])


def _balanced(m: _Measured, max_w: float) -> list[Line]:
    """Наименьшее число строк (до четырёх), при котором самая длинная строка
    минимальна и влезает в max_w; не вышло — переносим жадно."""
    best: dict[tuple[int, int], tuple[list[tuple[int, int]], float]] = {}

    def split(start: int, n: int) -> tuple[list[tuple[int, int]], float]:
        """Слова с start на n строк так, чтобы самая длинная была минимальной: строки и её ширина."""
        end = len(m.words)
        if n == 1 or end - start <= 1 or end - start < n:
            return [(start, end)], m.width(start, end)
        if (start, n) not in best:
            lines, width = [], float("inf")
            for i in range(start + 1, end - n + 2):
                rest, rest_width = split(i, n - 1)
                w = max(m.width(start, i), rest_width)
                if w < width:
                    lines, width = [(start, i), *rest], w
            best[start, n] = lines, width
        return best[start, n]

    for n in range(1, min(len(m.words), 4) + 1):
        lines, width = split(0, n)
        if width <= max_w:
            return m.lines(lines)
    return _greedy(m, max_w)


def _fit(paragraphs: list[list[Word]], max_w: float, max_lines: int, size: int, min_size: int,
         balanced: bool = False) -> tuple[Font, list[Line]]:
    """Самый крупный кегль (от size вниз), при котором заголовок влезает в max_lines строк.

    Сначала пробуем уложить каждый абзац в одну строку, уменьшив кегль не больше чем на 15 %:
    чуть мельче, но строкой — лучше, чем крупно, но с переносом одного слова.
    """
    paragraphs = [p for p in paragraphs if p]
    wrap = _balanced if balanced else _greedy
    for s in range(size, int(size * 0.85) - 1, -1):
        font = get_font(TITLE_FONT, s)
        if all(_Measured(p, font).width(0, len(p)) <= max_w for p in paragraphs):
            return font, [list(p) for p in paragraphs]
    for s in range(size, min_size - 1, -2):
        font = get_font(TITLE_FONT, s)
        measured = [_Measured(p, font) for p in paragraphs]
        lines = [line for m in measured for line in wrap(m, max_w)]
        if len(lines) <= max_lines and all(_line_width(line, font) <= max_w for line in lines):
            break
    return font, lines


def _draw_lines(layer: Image.Image, lines: list[Line], font: Font, first_baseline: float,
                pitch: float, x: float | None = None, center: float | None = None) -> None:
    space = font.getlength(" ")
    for i, line in enumerate(lines):
        cx = x if x is not None else center - _line_width(line, font) / 2
        for j, (word, red) in enumerate(line):
            cx = draw_rich(layer, cx, first_baseline + i * pitch, word, font, RED if red else WHITE)
            if j < len(line) - 1:
                cx += space


# ---------- детали макета ----------

def _background(size: tuple[int, int]) -> np.ndarray:
    w, h = size
    rgb = np.empty((h, w, 3), dtype=np.uint8)
    rgb[:] = BG
    rgb[3::4] = SCANLINE
    return rgb


def _layer(rgb: np.ndarray, alpha: np.ndarray) -> Image.Image:
    return Image.fromarray(np.dstack([rgb, to_alpha(alpha)]), "RGBA")


def _header(layer: Image.Image, handle: bool = True) -> None:
    layer.alpha_composite(_avatar(64), (40, 40))
    draw = ImageDraw.Draw(layer)
    draw.text((120, 81), _brand.name, font=get_font(TITLE_FONT, 26), anchor="ls", fill=WHITE)
    if handle:
        font = label_font(400, 22)
        x = layer.width - 40 - tracked_width(_brand.handle, font, 0.8)
        draw_tracked(draw, x, 81, _brand.handle, font, 0.8, GRAY)


KICKER_SIZE, KICKER_SPACING = 21, 4.4


def _kicker_width(text: str) -> float:
    return 22 + tracked_width(text, label_font(600, KICKER_SIZE), KICKER_SPACING)


def _kicker(draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str) -> float:
    """«■ АНОНС» — красный квадрат и подпись вразрядку. Возвращает x конца."""
    font = label_font(600, KICKER_SIZE)
    top = baseline - cap(font) / 2 - 4.5
    draw.rectangle((x, top, x + 8, top + 8), fill=RED)
    return draw_tracked(draw, x + 22, baseline, text, font, KICKER_SPACING, RED)


def _arrow(draw: ImageDraw.ImageDraw, x: float, y: float, color: tuple) -> None:
    draw.line((x, y, x + 12, y), fill=color, width=1)
    draw.line((x + 8, y - 4, x + 12, y), fill=color, width=1)
    draw.line((x + 8, y + 4, x + 12, y), fill=color, width=1)


@dataclass
class Layout:
    size: tuple[int, int]
    rect: tuple[int, int, int, int]  # x, y, w, h — куда встаёт фото или видео
    gray: bool                       # фото под слоем чёрно-белое
    fg: Image.Image                  # слой шаблона поверх фото
    whole: bool = False              # фото не обрезается: поля вокруг — размытая копия кадра


# ---------- шаблоны ----------

def _news(card: Card, frame: tuple[int, int]) -> Layout:
    # окно под фото — во всю ширину, высота по кадру: от 16:9 до 1000×880 из макета, чтобы
    # карточка не вышла выше 4:5; текст под окном съезжает вместе с его низом
    fw, fh = frame
    x, y, w, h = rect = (40, 132, 1000, even(min(max(1000 * fh / fw, 1000 / MAX_WIDE), 880)))
    bottom = y + h  # в макете низ фото — 1012
    size = (1080, bottom + 338)
    fg = _layer(_background(size), 1 - rounded_mask(size, (x, y, x + w, y + h), 4))
    _header(fg)
    draw = ImageDraw.Draw(fg)
    draw.rectangle((40, bottom + 3, 219, bottom + 5), fill=RED)  # красная черта под фото

    end = _kicker(draw, 40, bottom + 53, "НОВОСТИ")
    draw.line((end + 18, bottom + 44, 1040, bottom + 44), fill=DIVIDER, width=1)

    # заголовок растёт вниз, под ним — «читай в посте»
    font, lines = _fit([card.words], 1000, 3, 50, 34)
    pitch = round(font.size * 1.06)
    first = bottom + 89 + cap(font)
    _draw_lines(fg, lines, font, first, pitch, x=40)
    below = first + pitch * (len(lines) - 1) + 52
    end = draw_tracked(draw, 41, below, "ЧИТАЙ В ПОСТЕ", label_font(400, 24), 1.7, GRAY)
    _arrow(draw, end + 17, below - 5, GRAY)
    return Layout(size, rect, False, fg, whole=True)


def _announce(card: Card, cover: Image.Image | None = None) -> Layout:
    size = (1080, 1080)
    rect = (0, 60, 640, 740)
    # фото наполовину прозрачное и растворяется в фоне вправо и вниз
    fade_x = np.interp(np.arange(size[0]), [0, 380, 640], [1, 1, 0])
    fade_y = np.interp(np.arange(size[1]), [0, 59.9, 60, 420, 800], [0, 0, 1, 1, 0])
    fg = _layer(_background(size), 1 - 0.5 * fade_y[:, None] * fade_x[None, :])
    _header(fg, handle=False)
    draw = ImageDraw.Draw(fg)

    # карточка справа: обложка, если её прислали, иначе вопрос — обложку ещё не показали
    box = (560, 120, 1000, 560)
    if cover:
        side = box[2] - box[0]
        img = ImageOps.fit(cover, (side, side), Image.LANCZOS).convert("RGBA")
        mask = rounded_mask(size, box, 12)[box[1]:box[3], box[0]:box[2]]
        img.putalpha(Image.fromarray(to_alpha(mask)))
        fg.alpha_composite(img, box[:2])
        draw.rounded_rectangle((560, 120, 999, 559), 12, outline=RED, width=2)
    else:
        draw.rounded_rectangle((560, 120, 999, 559), 12, fill=CARD, outline=RED, width=2)
        q = get_font(TITLE_FONT, 216)
        qb = q.getbbox("?")
        draw.text((779.5 - (qb[0] + qb[2]) / 2, 339.5 - (qb[1] + qb[3]) / 2), "?", font=q, fill=WHITE)

    # всё снизу вверх: плашка с артистом, заголовок, «■ АНОНС»
    if card.author:
        font = label_font(500, 21)
        tw = tracked_width(card.author, font, 4.2)
        px = 540 - (tw + 66) / 2
        draw.rounded_rectangle((px, 975, px + tw + 66, 1023), 24, outline=PILL_BORDER, width=1)
        draw_tracked(draw, 540 - tw / 2, 999 + cap(font) / 2, card.author, font, 4.2, LIGHT)
        last = 943
    else:
        last = 1000

    white = [wd for wd in card.words if not wd[1]]
    red = [wd for wd in card.words if wd[1]]
    # выделен хвост — он встаёт отдельной красной строкой, как в макете; иначе — одним абзацем
    paragraphs = [white, red] if card.words == white + red else [card.words]
    font, lines = _fit(paragraphs, 920, 3, 80, 44, balanced=True)
    pitch = round(font.size * 1.05)
    first = last - pitch * (len(lines) - 1)
    _draw_lines(fg, lines, font, first, pitch, center=540)
    _kicker(draw, 540 - _kicker_width("АНОНС") / 2, first - cap(font) - 40, "АНОНС")
    return Layout(size, rect, False, fg)


def _quote(card: Card) -> Layout:
    size = (1080, 1080)
    rect = (0, 0, 1080, 1080)
    # чёрно-белое фото еле проступает, сверху и снизу ещё темнее
    fade = np.interp(np.arange(size[1]), [0, 220, 560, 840, 1080], [0.35, 1, 1, 0.35, 0.1])
    alpha = np.broadcast_to(1 - 0.45 * fade[:, None], (size[1], size[0]))
    fg = _layer(_background(size), alpha)
    _header(fg)
    draw = ImageDraw.Draw(fg)

    words = list(card.words)
    if words:  # кавычки прилипают к крайним словам, чтобы не остаться на строке одни
        words[0] = ("«" + words[0][0], words[0][1])
        words[-1] = (words[-1][0] + "»", words[-1][1])
    font, lines = _fit([words], 900, 4, 54, 34)
    pitch = round(font.size * 1.12)
    cap_h = cap(font)

    if card.author:
        draw_tracked(draw, 96, 1009, f"— {card.author}", label_font(500, 22), 4.0, LIGHT)
        last = 950
    else:
        last = 1000
    first = last - pitch * (len(lines) - 1)
    _draw_lines(fg, lines, font, first, pitch, x=97)
    draw.rectangle((64, first - cap_h - 11, 65, last + 7), fill=RED)
    _kicker(draw, 64, first - cap_h - 41, "ЦИТАТА")
    return Layout(size, rect, True, fg)


def _releases(covers: list[Image.Image]) -> Image.Image:
    """Релизы дня: заголовок из макета и до трёх обложек; первая — по центру, крупнее."""
    size = (1080, 1080)
    img = _layer(_background(size), np.ones((size[1], size[0]), dtype=np.float32))
    _header(img)
    draw = ImageDraw.Draw(img)

    title = label_font(600, 138)
    draw.text((540, 284), "ЭТИ РЕЛИЗЫ", font=title, anchor="ms", fill=WHITE)
    draw.text((44, 419), "ВЫШЛИ", font=title, anchor="ls", fill=RED)
    draw.text((44, 554), "СЕГОДНЯ", font=title, anchor="ls", fill=RED)

    # (x, y, сторона, главная)
    slots = {
        1: [(360, 612, 360, True)],
        2: [(166, 612, 360, True), (554, 612, 360, False)],
        3: [(360, 612, 360, True), (42, 646, 290, False), (748, 646, 290, False)],
    }[len(covers)]
    for cover, (x, y, s, main) in zip(covers, slots):
        img.paste(ImageOps.fit(cover, (s, s), Image.LANCZOS), (x, y))
        if main:
            draw.rectangle((x, y, x + s - 1, y + s - 1), outline=RED, width=2)
            draw.rectangle((x + 2, y + 2, x + s - 3, y + s - 3), outline=DARK_RED, width=1)
        else:
            draw.rectangle((x, y, x + s - 1, y + s - 1), outline=CARD_BORDER, width=1)

    font = label_font(400, 23)
    text = "СЛУШАЙ ПО ССЫЛКАМ В ПОСТЕ"
    tw = tracked_width(text, font, 3.4)
    tx = 540 - tw / 2
    draw_tracked(draw, tx, 1048, text, font, 3.4, GRAY)
    draw.line((40, 1035, tx - 18, 1035), fill=DIVIDER, width=1)
    draw.line((tx + tw + 18, 1035, 1040, 1035), fill=DIVIDER, width=1)
    return img


def card_layout(mode: str, card: Card, frame: tuple[int, int], cover: ImageSource | None = None) -> Layout:
    """Слой шаблона для фото или видео (кроме релизов — они собираются только из фото).

    frame — ширина и высота кадра: под неё подстраивается окно новости.
    cover — обложка для карточки анонса вместо «?».
    """
    if mode == "news":
        return _news(card, frame)
    if mode == "announce":
        return _announce(card, open_image(cover) if cover else None)
    return _quote(card)


def layout_png(layout: Layout) -> bytes:
    out = io.BytesIO()
    layout.fg.save(out, "PNG")
    return out.getvalue()


# ---------- готовые картинки ----------

def render_card(mode: str, card: Card, photos: list[ImageSource]) -> bytes:
    """JPEG карточки. Для релизов photos — обложки (до трёх), для анонса — фото и, если есть,
    обложка, для остальных — одно фото."""
    if mode == "releases":
        return to_jpeg(_releases([open_image(p) for p in photos[:RELEASES_MAX]]))
    photo = open_image(photos[0])
    layout = card_layout(mode, card, photo.size, photos[1] if mode == "announce" and len(photos) > 1 else None)
    x, y, w, h = layout.rect
    img = fit_whole(photo, (w, h)) if layout.whole else ImageOps.fit(photo, (w, h), Image.LANCZOS)
    if layout.gray:
        img = ImageOps.grayscale(img).convert("RGB")
    canvas = Image.new("RGBA", layout.size, (0, 0, 0, 255))
    canvas.paste(img, (x, y))
    canvas.alpha_composite(layout.fg)
    return to_jpeg(canvas)


def render_plain(photo: ImageSource, size: tuple[int, int] | None = None) -> bytes:
    """JPEG без оформления: фото целиком, слишком высокое или широкое — с размытыми полями.

    size — точный размер результата (слайды карусели равняются на первый); по умолчанию — по кадру.
    """
    img = open_image(photo)
    return to_jpeg(fit_whole(img, size or canvas_size(*img.size)))
