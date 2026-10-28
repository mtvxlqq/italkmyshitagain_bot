"""Картинки «Переписка»: пузыри с ником, текстом, временем и аватаркой на фоне из макета."""

import io
import re
from dataclasses import dataclass, replace
from functools import cache, lru_cache

from PIL import Image, ImageDraw, ImageOps

from .common import ASSETS, GRAY, RED, WHITE, circle, rounded_mask, to_alpha, to_jpeg
from .text import FONTS, Font, drawable, draw_rich, get_font, text_width

CHAT_BG = ASSETS / "chat_bg.png"  # фон из макета: шапка, панель под сообщения, подвал
CHAT_FONT = FONTS / "Inter-Regular.ttf"
CHAT_NAME_FONT = FONTS / "Inter-Medium.ttf"
# цвет ника — по порядку появления собеседника; первые два из макета
CHAT_NAME_COLORS = [RED, (217, 164, 65), (98, 160, 196), (150, 122, 200), (112, 174, 122)]
BUBBLE = (36, 33, 32)
AVATAR_STUB = (58, 54, 52)
CHAT_PAGES_MAX = 10  # картинок в альбоме Telegram

# размеры из макета
CHAT_TOP, CHAT_BOTTOM = 192, 1200  # сообщения — по центру этой полосы внутри панели
BUBBLE_X, BUBBLE_PAD, BUBBLE_RADIUS = 175, 24, 16
BUBBLE_TEXT_MAX = 638  # сообщение с переносами растягивает пузырь на всю эту ширину
AVATAR_X, AVATAR_SIZE = 105, 56
LINE_PITCH = 40.5
NAME_BASELINE = 40      # от верха пузыря
TEXT_AFTER_NAME = 41    # от базовой линии ника до первой строки
TEXT_NO_NAME = 42       # первая строка, когда ника нет (второе сообщение подряд)
TIME_AFTER_TEXT = 34
BUBBLE_BOTTOM = 23      # от базовой линии времени до низа пузыря
GAP_OTHER, GAP_SAME = 23, 6  # между пузырями разных собеседников и одного


@dataclass
class ChatMessage:
    sender: str
    key: str            # один собеседник — один цвет, даже если имя совпало с чужим
    time: str           # «21:04»
    text: str = ""
    note: str = ""      # «Фото», «Стикер» — вместо вложения, серым
    avatar: bytes | None = None  # аватарка собеседника; нет — серый кружок


ChatLine = tuple[str, tuple]  # строка и её цвет


def _text_font() -> Font:
    return get_font(CHAT_FONT, 29)


def _wrap(text: str, font: Font, max_w: float) -> list[str]:
    """Переносит по словам с сохранением абзацев; слишком длинное слово режет по буквам."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if text_width(candidate, font) <= max_w:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for ch in word:
                if line and text_width(line + ch, font) > max_w:
                    lines.append(line)
                    line = ""
                line += ch
        lines.append(line)
    return lines


def _chat_text(text: str) -> str:
    text = drawable(text, CHAT_FONT)
    text = "\n".join(re.sub(r"\s+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass
class _Bubble:
    msg: ChatMessage
    color: tuple
    lines: list[ChatLine]
    wide: bool = False   # были переносы — пузырь во всю ширину, как в макете
    name: bool = True    # ник показываем у первого сообщения собеседника подряд
    avatar: bool = True  # аватарку — у последнего

    def first_baseline(self) -> int:
        return NAME_BASELINE + TEXT_AFTER_NAME if self.name else TEXT_NO_NAME

    def height(self) -> int:
        return round(self.first_baseline() + LINE_PITCH * (len(self.lines) - 1) + TIME_AFTER_TEXT + BUBBLE_BOTTOM)


def _chat_bubbles(messages: list[ChatMessage]) -> list[_Bubble]:
    font = _text_font()
    colors: dict[str, tuple] = {}
    bubbles = []
    for msg in messages:
        color = colors.setdefault(msg.key, CHAT_NAME_COLORS[len(colors) % len(CHAT_NAME_COLORS)])
        sender = re.sub(r"\s+", " ", drawable(msg.sender, CHAT_NAME_FONT)).strip() or "Без имени"
        lines: list[ChatLine] = []
        paragraphs = 0
        for text, fill in ((msg.note, GRAY), (_chat_text(msg.text), WHITE)):
            if text:
                lines += [(line, fill) for line in _wrap(text, font, BUBBLE_TEXT_MAX)]
                paragraphs += text.count("\n") + 1
        if lines:
            bubbles.append(_Bubble(replace(msg, sender=sender), color, lines, wide=len(lines) > paragraphs))
    return bubbles


def _mark_groups(page: list[_Bubble]) -> None:
    for i, b in enumerate(page):
        b.name = i == 0 or page[i - 1].msg.key != b.msg.key
        b.avatar = i == len(page) - 1 or page[i + 1].msg.key != b.msg.key


def _page_height(page: list[_Bubble]) -> int:
    _mark_groups(page)
    gaps = sum(GAP_SAME if not b.name else GAP_OTHER for b in page[1:])
    return sum(b.height() for b in page) + gaps


def _paginate(bubbles: list[_Bubble]) -> list[list[_Bubble]]:
    """Раскладывает пузыри по картинкам; сообщение выше целой панели делится на несколько пузырей."""
    room = CHAT_BOTTOM - CHAT_TOP
    pages: list[list[_Bubble]] = [[]]
    queue = list(bubbles)
    while queue:
        b = queue.pop(0)
        if _page_height(pages[-1] + [b]) <= room:
            pages[-1].append(b)
            continue
        if pages[-1]:
            pages.append([])
        if _page_height([b]) > room:
            fit = int((room - _Bubble(b.msg, b.color, b.lines[:1]).height()) // LINE_PITCH) + 1
            queue.insert(0, _Bubble(b.msg, b.color, b.lines[fit:], b.wide))
            b = _Bubble(b.msg, b.color, b.lines[:fit], b.wide)
        pages[-1].append(b)
    return [p for p in pages if p]


@cache
def _background() -> Image.Image:
    return Image.open(CHAT_BG).convert("RGBA")


@lru_cache(maxsize=16)
def _avatar(data: bytes | None) -> Image.Image:
    """Аватарка-кружок; нет её или не открылась — серый кружок."""
    face = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), AVATAR_STUB)
    if data:
        try:
            face = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGBA"), face.size, Image.LANCZOS)
        except OSError:
            pass
    face.putalpha(circle(AVATAR_SIZE))
    return face


def _draw_bubble(img: Image.Image, b: _Bubble, y: int) -> None:
    text_font = _text_font()
    name_font = get_font(CHAT_NAME_FONT, 21)
    time_font = get_font(CHAT_FONT, 17)
    width = BUBBLE_TEXT_MAX if b.wide else max(
        [text_width(line, text_font) for line, _ in b.lines]
        + [time_font.getlength(b.msg.time)]
        + ([text_width(b.msg.sender, name_font)] if b.name else [])
    )
    w, h = round(min(width, BUBBLE_TEXT_MAX) + BUBBLE_PAD * 2), b.height()

    bubble = Image.new("RGBA", (w, h), BUBBLE)
    bubble.putalpha(Image.fromarray(to_alpha(rounded_mask((w, h), (0, 0, w, h), BUBBLE_RADIUS))))
    img.alpha_composite(bubble, (BUBBLE_X, y))
    if b.avatar:
        img.alpha_composite(_avatar(b.msg.avatar), (AVATAR_X, y + h - AVATAR_SIZE))

    x = BUBBLE_X + BUBBLE_PAD
    if b.name:
        draw_rich(img, x, y + NAME_BASELINE, b.msg.sender, name_font, b.color)
    baseline = y + b.first_baseline()
    for i, (line, color) in enumerate(b.lines):
        draw_rich(img, x, round(baseline + i * LINE_PITCH), line, text_font, color)
    last = round(baseline + LINE_PITCH * (len(b.lines) - 1))
    ImageDraw.Draw(img).text((BUBBLE_X + w - BUBBLE_PAD, last + TIME_AFTER_TEXT), b.msg.time,
                             font=time_font, anchor="rs", fill=GRAY)


def render_chat(messages: list[ChatMessage]) -> list[bytes]:
    """JPEG-картинки переписки: пузыри с никами и временем на фоне из макета.

    Не влезло на одну картинку — продолжение на следующих. Пустой список — рисовать нечего
    (например, в сообщениях были только символы, которых нет в шрифте).
    """
    pages = []
    for page in _paginate(_chat_bubbles(messages)):
        img = _background().copy()
        y = CHAT_TOP + (CHAT_BOTTOM - CHAT_TOP - _page_height(page)) // 2
        for i, b in enumerate(page):
            if i:
                y += GAP_OTHER if b.name else GAP_SAME
            _draw_bubble(img, b, y)
            y += b.height()
        pages.append(to_jpeg(img))
    return pages
