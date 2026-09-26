"""Оформление постов по шаблонам канала: новость, анонс, релизы, цитата — и режим без оверлея.

Шаблон — это непрозрачный слой поверх фото: фон со сканлайнами, шапка канала, надписи.
На месте фото в слое «окно» (полная или частичная прозрачность), поэтому один и тот же
слой годится и для фото (склеиваем в PIL), и для видео (ffmpeg кладёт его поверх кадра).
"""

import io
import math
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

ASSETS = Path(__file__).parent / "assets"
FONTS = ASSETS / "fonts"
TITLE_FONT = FONTS / "Unbounded-Bold.ttf"
LABEL_FONTS = {400: FONTS / "Oswald-Regular.ttf", 500: FONTS / "Oswald-Medium.ttf", 600: FONTS / "Oswald-SemiBold.ttf"}
AVATAR_DEFAULT = ASSETS / "avatar.png"
# эмодзи Apple, 96×96, файлы названы кодом символа: 1f602.png = 😂
EMOJI_DIR = ASSETS / "emoji"
EMOJI = {chr(int(p.stem, 16)): p for p in EMOJI_DIR.glob("*.png")}
EMOJI_SCALE = 1.25  # высота эмодзи относительно высоты заглавной буквы
EMOJI_ADVANCE = 1.1  # ширина места под эмодзи относительно его размера — чтобы соседние не слипались

# цвета из макетов
BG = (17, 17, 17)
SCANLINE = (20, 20, 20)  # каждая четвёртая строка фона чуть светлее
WHITE = (236, 231, 225)
RED = (190, 58, 52)
DARK_RED = (145, 49, 45)
GRAY = (142, 136, 131)     # @канал, «читай в посте», подвал
LIGHT = (181, 175, 169)    # артист, автор цитаты
CARD = (27, 26, 25)
CARD_BORDER = (44, 42, 41)
PILL_BORDER = (61, 58, 56)
DIVIDER = (40, 38, 37)

# режимы: ключ — подпись на кнопке
MODES = {
    "news": "📰 Новость",
    "announce": "📣 Анонс",
    "releases": "💿 Релизы",
    "quote": "💬 Цитата",
    "plain": "🖼 Без оверлея",
}
RELEASES_MAX = 3  # обложек на карточке релизов

# ---------- режим без оверлея ----------

SIDE = 1080  # короткая сторона результата
SQUARE = (SIDE, SIDE)
PORTRAIT = (SIDE, SIDE * 4 // 3)      # 3:4 — вертикальные кадры
LANDSCAPE = (SIDE * 4 // 3, SIDE)     # 4:3 — умеренно горизонтальные
WIDESCREEN = (SIDE * 16 // 9, SIDE)   # 16:9 — широкие


def canvas_size(width: int, height: int) -> tuple[int, int]:
    """Вертикальные кадры — 3:4; горизонтальные — ближайший из 1:1, 4:3 и 16:9."""
    if height > width:
        return PORTRAIT
    ratio = width / height
    if ratio < 1.15:
        return SQUARE
    # граница — середина между 4:3 (1.33) и 16:9 (1.78)
    return LANDSCAPE if ratio < 1.555 else WIDESCREEN


def _open(photo: bytes) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")


def _jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=95, subsampling=0)
    return out.getvalue()


def render_plain(photo: bytes) -> bytes:
    """JPEG без оформления: фото, обрезанное по центру под ближайший формат кадра."""
    img = _open(photo)
    return _jpeg(ImageOps.fit(img, canvas_size(*img.size), Image.LANCZOS))


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


# ---------- текст ----------

@cache
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _label_font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    return _font(LABEL_FONTS[weight], size)


def _cap(font: ImageFont.FreeTypeFont) -> int:
    box = font.getbbox("Н")
    return box[3] - box[1]


def _glyph(ch: str) -> bytes:
    img = Image.new("L", (80, 80))
    ImageDraw.Draw(img).text((10, 10), ch, font=_font(TITLE_FONT, 50), fill=255)
    return img.tobytes()


@cache
def _in_font(ch: str) -> bool:
    # символа нет в шрифте — рисуется так же, как заведомо отсутствующий (пустой прямоугольник);
    # пустой глиф — невидимый служебный символ
    glyph = _glyph(ch)
    return glyph != _glyph("\U0010FFFD") and any(glyph)


# keycap-эмодзи (1️⃣, #️⃣): цифра сама по себе есть в шрифте, убираем её вместе с рамкой
_KEYCAP = re.compile("[0-9#*]️?⃣")
# селекторы вариантов (❤️ = ❤ + U+FE0F) — сами по себе невидимы
_VARIATION_SELECTOR = re.compile("[︀-️\U000E0100-\U000E01EF]")


def clean_title(title: str) -> str:
    """Убирает символы, которых нет в шрифте. Эмодзи из assets/emoji остаются."""
    title = _KEYCAP.sub(" ", title)
    chars = []
    for ch in title:
        if ch.isspace():
            chars.append(" ")
        elif ch in EMOJI:
            chars.append(ch)
        elif not (unicodedata.category(ch).startswith("C") or _VARIATION_SELECTOR.match(ch)) and _in_font(ch):
            chars.append(ch)
    return re.sub(r"\s+", " ", "".join(chars)).strip().upper()


def _segments(text: str) -> list[str]:
    """Текст по кускам: буквы отдельно, каждое эмодзи отдельно."""
    return [seg for seg in re.split(f"([{''.join(EMOJI)}])", text) if seg] if EMOJI else [text]


def _emoji_size(font: ImageFont.FreeTypeFont) -> int:
    return round(_cap(font) * EMOJI_SCALE)


def _width(text: str, font: ImageFont.FreeTypeFont) -> float:
    size = _emoji_size(font)
    return sum(size * EMOJI_ADVANCE if seg in EMOJI else font.getlength(seg) for seg in _segments(text))


@cache
def _emoji_image(ch: str, size: int) -> Image.Image:
    return Image.open(EMOJI[ch]).convert("RGBA").resize((size, size), Image.LANCZOS)


def _draw_rich(layer: Image.Image, x: float, baseline: float, text: str,
               font: ImageFont.FreeTypeFont, fill: tuple) -> float:
    """Текст с эмодзи; эмодзи — по центру высоты заглавных. Возвращает x после текста."""
    draw = ImageDraw.Draw(layer)
    for seg in _segments(text):
        if seg in EMOJI:
            size = _emoji_size(font)
            pos = (round(x + size * (EMOJI_ADVANCE - 1) / 2), round(baseline - _cap(font) / 2 - size / 2))
            layer.alpha_composite(_emoji_image(seg, size), pos)
            x += size * EMOJI_ADVANCE
        else:
            draw.text((x, baseline), seg, font=font, anchor="ls", fill=fill)
            x += font.getlength(seg)
    return x


def _tracked_width(text: str, font: ImageFont.FreeTypeFont, spacing: float) -> float:
    return sum(font.getlength(ch) for ch in text) + spacing * (len(text) - 1)


def _draw_tracked(draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str,
                  font: ImageFont.FreeTypeFont, spacing: float, fill: tuple) -> float:
    """Подпись вразрядку (в макетах у узкого шрифта увеличен межбуквенный интервал). Возвращает x конца."""
    for ch in text:
        draw.text((x, baseline), ch, font=font, anchor="ls", fill=fill)
        x += font.getlength(ch) + spacing
    return x - spacing


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


def _line_width(line: Line, font: ImageFont.FreeTypeFont) -> float:
    return sum(_width(w, font) for w, _ in line) + font.getlength(" ") * (len(line) - 1)


def _greedy(words: list[Word], font: ImageFont.FreeTypeFont, max_w: float) -> list[Line]:
    lines: list[Line] = [[]]
    for word in words:
        if lines[-1] and _line_width(lines[-1] + [word], font) > max_w:
            lines.append([])
        lines[-1].append(word)
    return lines


def _split_balanced(words: list[Word], n: int, font: ImageFont.FreeTypeFont) -> list[Line]:
    """Делит слова на n строк так, чтобы самая длинная была минимальной."""
    if n == 1 or len(words) <= 1:
        return [words]
    best, best_width = [words], float("inf")
    for i in range(1, len(words) - n + 2):
        lines = [words[:i], *_split_balanced(words[i:], n - 1, font)]
        width = max(_line_width(line, font) for line in lines)
        if width < best_width:
            best, best_width = lines, width
    return best


def _balanced(words: list[Word], font: ImageFont.FreeTypeFont, max_w: float) -> list[Line]:
    for n in range(1, min(len(words), 4) + 1):
        lines = _split_balanced(words, n, font)
        if max(_line_width(line, font) for line in lines) <= max_w:
            return lines
    return _greedy(words, font, max_w)


def _fit(paragraphs: list[list[Word]], max_w: float, max_lines: int, size: int, min_size: int,
         balanced: bool = False) -> tuple[ImageFont.FreeTypeFont, list[Line]]:
    """Самый крупный кегль (от size вниз), при котором заголовок влезает в max_lines строк.

    Сначала пробуем уложить каждый абзац в одну строку, уменьшив кегль не больше чем на 15 %:
    чуть мельче, но строкой — лучше, чем крупно, но с переносом одного слова.
    """
    wrap = _balanced if balanced else _greedy
    for s in range(size, int(size * 0.85) - 1, -1):
        font = _font(TITLE_FONT, s)
        lines = [[w for w in p] for p in paragraphs if p]
        if all(_line_width(line, font) <= max_w for line in lines):
            return font, lines
    for s in range(size, min_size - 1, -2):
        font = _font(TITLE_FONT, s)
        lines = [line for p in paragraphs if p for line in wrap(p, font, max_w)]
        if len(lines) <= max_lines and all(_line_width(line, font) <= max_w for line in lines):
            break
    return font, lines


def _draw_lines(layer: Image.Image, lines: list[Line], font: ImageFont.FreeTypeFont, first_baseline: float,
                pitch: float, x: float | None = None, center: float | None = None) -> None:
    space = font.getlength(" ")
    for i, line in enumerate(lines):
        cx = x if x is not None else center - _line_width(line, font) / 2
        for j, (word, red) in enumerate(line):
            cx = _draw_rich(layer, cx, first_baseline + i * pitch, word, font, RED if red else WHITE)
            if j < len(line) - 1:
                cx += space


# ---------- детали макета ----------

def _background(size: tuple[int, int]) -> np.ndarray:
    w, h = size
    rgb = np.empty((h, w, 3), dtype=np.float32)
    rgb[:] = BG
    rgb[3::4] = SCANLINE
    return rgb


def _layer(rgb: np.ndarray, alpha: np.ndarray) -> Image.Image:
    out = np.dstack([rgb, alpha[..., None] * 255]).clip(0, 255).round().astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _header(layer: Image.Image, handle: bool = True) -> None:
    layer.alpha_composite(_avatar(64), (40, 40))
    draw = ImageDraw.Draw(layer)
    draw.text((120, 81), _brand.name, font=_font(TITLE_FONT, 26), anchor="ls", fill=WHITE)
    if handle:
        font = _label_font(400, 22)
        x = layer.width - 40 - _tracked_width(_brand.handle, font, 0.8)
        _draw_tracked(draw, x, 81, _brand.handle, font, 0.8, GRAY)


KICKER_SIZE, KICKER_SPACING = 21, 4.4


def _kicker_width(text: str) -> float:
    return 22 + _tracked_width(text, _label_font(600, KICKER_SIZE), KICKER_SPACING)


def _kicker(draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str) -> float:
    """«■ АНОНС» — красный квадрат и подпись вразрядку. Возвращает x конца."""
    font = _label_font(600, KICKER_SIZE)
    top = baseline - _cap(font) / 2 - 4.5
    draw.rectangle((x, top, x + 8, top + 8), fill=RED)
    return _draw_tracked(draw, x + 22, baseline, text, font, KICKER_SPACING, RED)


def _arrow(draw: ImageDraw.ImageDraw, x: float, y: float, color: tuple) -> None:
    draw.line((x, y, x + 12, y), fill=color, width=1)
    draw.line((x + 8, y - 4, x + 12, y), fill=color, width=1)
    draw.line((x + 8, y + 4, x + 12, y), fill=color, width=1)


def _rounded_mask(size: tuple[int, int], box: tuple[int, int, int, int], radius: int) -> np.ndarray:
    """Маска 0…1: 1 внутри скруглённого прямоугольника (сглаженные углы)."""
    k = 4
    big = Image.new("L", (size[0] * k, size[1] * k))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(big).rounded_rectangle((x0 * k, y0 * k, x1 * k - 1, y1 * k - 1), radius * k, fill=255)
    return np.asarray(big.resize(size, Image.BOX), dtype=np.float32) / 255


@dataclass
class Layout:
    size: tuple[int, int]
    rect: tuple[int, int, int, int]  # x, y, w, h — куда встаёт фото или видео
    gray: bool                       # фото под слоем чёрно-белое
    fg: Image.Image                  # слой шаблона поверх фото


# ---------- шаблоны ----------

def _news(card: Card) -> Layout:
    size = (1080, 1350)
    rect = (40, 132, 1000, 880)
    x, y, w, h = rect
    fg = _layer(_background(size), 1 - _rounded_mask(size, (x, y, x + w, y + h), 4))
    _header(fg)
    draw = ImageDraw.Draw(fg)
    draw.rectangle((40, 1015, 219, 1017), fill=RED)  # красная черта под фото

    end = _kicker(draw, 40, 1065, "НОВОСТИ")
    draw.line((end + 18, 1056, 1040, 1056), fill=DIVIDER, width=1)

    # заголовок растёт вниз, под ним — «читай в посте»
    font, lines = _fit([card.words], 1000, 3, 50, 34)
    pitch = round(font.size * 1.06)
    first = 1101 + _cap(font)
    _draw_lines(fg, lines, font, first, pitch, x=40)
    below = first + pitch * (len(lines) - 1) + 52
    end = _draw_tracked(draw, 41, below, "ЧИТАЙ В ПОСТЕ", _label_font(400, 24), 1.7, GRAY)
    _arrow(draw, end + 17, below - 5, GRAY)
    return Layout(size, rect, False, fg)


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
        mask = _rounded_mask(size, box, 12)[box[1]:box[3], box[0]:box[2]]
        img.putalpha(Image.fromarray((mask * 255).round().astype(np.uint8)))
        fg.alpha_composite(img, box[:2])
        draw.rounded_rectangle((560, 120, 999, 559), 12, outline=RED, width=2)
    else:
        draw.rounded_rectangle((560, 120, 999, 559), 12, fill=CARD, outline=RED, width=2)
        q = _font(TITLE_FONT, 216)
        qb = q.getbbox("?")
        draw.text((779.5 - (qb[0] + qb[2]) / 2, 339.5 - (qb[1] + qb[3]) / 2), "?", font=q, fill=WHITE)

    # всё снизу вверх: плашка с артистом, заголовок, «■ АНОНС»
    if card.author:
        font = _label_font(500, 21)
        tw = _tracked_width(card.author, font, 4.2)
        px = 540 - (tw + 66) / 2
        draw.rounded_rectangle((px, 975, px + tw + 66, 1023), 24, outline=PILL_BORDER, width=1)
        _draw_tracked(draw, 540 - tw / 2, 999 + _cap(font) / 2, card.author, font, 4.2, LIGHT)
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
    _kicker(draw, 540 - _kicker_width("АНОНС") / 2, first - _cap(font) - 40, "АНОНС")
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
    cap = _cap(font)

    if card.author:
        _draw_tracked(draw, 96, 1009, f"— {card.author}", _label_font(500, 22), 4.0, LIGHT)
        last = 950
    else:
        last = 1000
    first = last - pitch * (len(lines) - 1)
    _draw_lines(fg, lines, font, first, pitch, x=97)
    draw.rectangle((64, first - cap - 11, 65, last + 7), fill=RED)
    _kicker(draw, 64, first - cap - 41, "ЦИТАТА")
    return Layout(size, rect, True, fg)


def _releases(covers: list[Image.Image]) -> Image.Image:
    """Релизы дня: заголовок из макета и до трёх обложек; первая — по центру, крупнее."""
    size = (1080, 1080)
    img = _layer(_background(size), np.ones((size[1], size[0]), dtype=np.float32))
    _header(img)
    draw = ImageDraw.Draw(img)

    title = _label_font(600, 138)
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

    font = _label_font(400, 23)
    text = "СЛУШАЙ ПО ССЫЛКАМ В ПОСТЕ"
    tw = _tracked_width(text, font, 3.4)
    tx = 540 - tw / 2
    _draw_tracked(draw, tx, 1048, text, font, 3.4, GRAY)
    draw.line((40, 1035, tx - 18, 1035), fill=DIVIDER, width=1)
    draw.line((tx + tw + 18, 1035, 1040, 1035), fill=DIVIDER, width=1)
    return img


_TEMPLATES = {"news": _news, "announce": _announce, "quote": _quote}


def card_layout(mode: str, card: Card, cover: bytes | None = None) -> Layout:
    """Слой шаблона для фото или видео (кроме релизов — они собираются только из фото).

    cover — обложка для карточки анонса вместо «?».
    """
    if mode == "announce":
        return _announce(card, _open(cover) if cover else None)
    return _TEMPLATES[mode](card)


def layout_png(layout: Layout) -> bytes:
    out = io.BytesIO()
    layout.fg.save(out, "PNG")
    return out.getvalue()


# ---------- публичное API ----------

def render_card(mode: str, card: Card, photos: list[bytes]) -> bytes:
    """JPEG карточки. Для релизов photos — обложки (до трёх), для анонса — фото и, если есть,
    обложка, для остальных — одно фото."""
    if mode == "releases":
        return _jpeg(_releases([_open(p) for p in photos[:RELEASES_MAX]]))
    layout = card_layout(mode, card, photos[1] if mode == "announce" and len(photos) > 1 else None)
    x, y, w, h = layout.rect
    img = ImageOps.fit(_open(photos[0]), (w, h), Image.LANCZOS)
    if layout.gray:
        img = ImageOps.grayscale(img).convert("RGB")
    canvas = Image.new("RGBA", layout.size, (0, 0, 0, 255))
    canvas.paste(img, (x, y))
    canvas.alpha_composite(layout.fg)
    return _jpeg(canvas)
