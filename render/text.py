"""Текст на картинках: шрифты, эмодзи Apple, измерение и рисование строк."""

import re
import unicodedata
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .common import ASSETS

FONTS = ASSETS / "fonts"
TITLE_FONT = FONTS / "Unbounded-Bold.ttf"
LABEL_FONTS = {400: FONTS / "Oswald-Regular.ttf", 500: FONTS / "Oswald-Medium.ttf", 600: FONTS / "Oswald-SemiBold.ttf"}
# эмодзи Apple, 96×96, файлы названы кодом символа: 1f602.png = 😂
EMOJI_DIR = ASSETS / "emoji"
EMOJI = {chr(int(p.stem, 16)): p for p in EMOJI_DIR.glob("*.png")}
EMOJI_SCALE = 1.25  # высота эмодзи относительно высоты заглавной буквы
EMOJI_ADVANCE = 1.1  # ширина места под эмодзи относительно его размера — чтобы соседние не слипались

Font = ImageFont.FreeTypeFont


@cache
def get_font(path: Path, size: int) -> Font:
    return ImageFont.truetype(str(path), size)


def label_font(weight: int, size: int) -> Font:
    return get_font(LABEL_FONTS[weight], size)


@cache
def cap(font: Font) -> int:
    """Высота заглавной буквы."""
    box = font.getbbox("Н")
    return box[3] - box[1]


# ---------- какие символы можно нарисовать ----------

def _glyph(ch: str, path: Path) -> bytes:
    img = Image.new("L", (80, 80))
    ImageDraw.Draw(img).text((10, 10), ch, font=get_font(path, 50), fill=255)
    return img.tobytes()


@cache
def _missing_glyph(path: Path) -> bytes:
    """Как шрифт рисует символ, которого в нём нет."""
    return _glyph("\U0010FFFD", path)


@cache
def _in_font(ch: str, path: Path) -> bool:
    # символа нет в шрифте — рисуется так же, как заведомо отсутствующий (пустой прямоугольник);
    # пустой глиф — невидимый служебный символ
    glyph = _glyph(ch, path)
    return glyph != _missing_glyph(path) and any(glyph)


# keycap-эмодзи (1️⃣, #️⃣): цифра сама по себе есть в шрифте, убираем её вместе с рамкой
_KEYCAP = re.compile("[0-9#*]️?⃣")
# селекторы вариантов (❤️ = ❤ + U+FE0F) — сами по себе невидимы
_VARIATION_SELECTOR = re.compile("[︀-️\U000E0100-\U000E01EF]")


def drawable(text: str, path: Path) -> str:
    """Оставляет пробелы, эмодзи из assets/emoji и символы, которые есть в шрифте."""
    chars = []
    for ch in _KEYCAP.sub(" ", text):
        if ch.isspace() or ch in EMOJI:
            chars.append(ch)
        elif not (unicodedata.category(ch).startswith("C") or _VARIATION_SELECTOR.match(ch)) and _in_font(ch, path):
            chars.append(ch)
    return "".join(chars)


def clean_title(title: str) -> str:
    """Убирает символы, которых нет в шрифте. Эмодзи из assets/emoji остаются."""
    return re.sub(r"\s+", " ", drawable(title, TITLE_FONT)).strip().upper()


# ---------- строка с эмодзи ----------

_EMOJI_RE = re.compile(f"([{''.join(map(re.escape, EMOJI))}])") if EMOJI else None


def _segments(text: str) -> list[str]:
    """Текст по кускам: буквы отдельно, каждое эмодзи отдельно."""
    return [seg for seg in _EMOJI_RE.split(text) if seg] if _EMOJI_RE else [text]


def _emoji_size(font: Font) -> int:
    return round(cap(font) * EMOJI_SCALE)


def text_width(text: str, font: Font) -> float:
    size = _emoji_size(font)
    return sum(size * EMOJI_ADVANCE if seg in EMOJI else font.getlength(seg) for seg in _segments(text))


@cache
def _emoji_image(ch: str, size: int) -> Image.Image:
    return Image.open(EMOJI[ch]).convert("RGBA").resize((size, size), Image.LANCZOS)


def draw_rich(layer: Image.Image, x: float, baseline: float, text: str, font: Font, fill: tuple) -> float:
    """Текст с эмодзи; эмодзи — по центру высоты заглавных. Возвращает x после текста."""
    draw = ImageDraw.Draw(layer)
    for seg in _segments(text):
        if seg in EMOJI:
            size = _emoji_size(font)
            pos = (round(x + size * (EMOJI_ADVANCE - 1) / 2), round(baseline - cap(font) / 2 - size / 2))
            layer.alpha_composite(_emoji_image(seg, size), pos)
            x += size * EMOJI_ADVANCE
        else:
            draw.text((x, baseline), seg, font=font, anchor="ls", fill=fill)
            x += font.getlength(seg)
    return x


# ---------- подписи вразрядку ----------

def tracked_width(text: str, font: Font, spacing: float) -> float:
    return sum(font.getlength(ch) for ch in text) + spacing * (len(text) - 1)


def draw_tracked(draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str,
                 font: Font, spacing: float, fill: tuple) -> float:
    """Подпись вразрядку (в макетах у узкого шрифта увеличен межбуквенный интервал). Возвращает x конца."""
    for ch in text:
        draw.text((x, baseline), ch, font=font, anchor="ls", fill=fill)
        x += font.getlength(ch) + spacing
    return x - spacing
