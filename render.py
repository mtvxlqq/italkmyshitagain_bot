"""Оформление: фото/видео + оверлей + заголовок."""

import io
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ASSETS = Path(__file__).parent / "assets"
OVERLAY_SRC = ASSETS / "overlay.png"
OVERLAY_CLEAN = ASSETS / "overlay_clean.png"
OVERLAY_META = ASSETS / "overlay_clean.json"
FONT_PATH = ASSETS / "fonts" / "Montserrat-ExtraBold.otf"
# эмодзи Apple, 96×96, файлы названы кодом символа: 1f602.png = 😂
EMOJI_DIR = ASSETS / "emoji"
EMOJI = {chr(int(p.stem, 16)): p for p in EMOJI_DIR.glob("*.png")}
EMOJI_SCALE = 1.25  # высота эмодзи относительно высоты заглавной буквы
EMOJI_ADVANCE = 1.1  # ширина места под эмодзи относительно его размера — чтобы соседние не слипались

SIDE = 1080  # короткая сторона результата
SQUARE = (SIDE, SIDE)
PORTRAIT = (SIDE, SIDE * 4 // 3)      # 3:4 — вертикальные кадры
LANDSCAPE = (SIDE * 4 // 3, SIDE)     # 4:3 — умеренно горизонтальные
WIDESCREEN = (SIDE * 16 // 9, SIDE)   # 16:9 — широкие
MAX_LINES = 3


@dataclass
class OverlayMeta:
    # доли от стороны квадратного оверлея
    text_cy: float
    text_bottom: float
    text_width: float
    cap_height: float


def canvas_size(width: int, height: int) -> tuple[int, int]:
    """Вертикальные кадры — 3:4; горизонтальные — ближайший из 1:1, 4:3 и 16:9."""
    if height > width:
        return PORTRAIT
    ratio = width / height
    if ratio < 1.15:
        return SQUARE
    # граница — середина между 4:3 (1.33) и 16:9 (1.78)
    return LANDSCAPE if ratio < 1.555 else WIDESCREEN


# ---------- подготовка оверлея ----------

def _poly_features(x: np.ndarray, y: np.ndarray, deg: int) -> np.ndarray:
    return np.stack([x**i * y**j for i in range(deg + 1) for j in range(deg + 1 - i)], axis=1)


def _remove_placeholder(rgb: np.ndarray, text_rows: tuple[int, int] | None) -> tuple[np.ndarray, np.ndarray]:
    """Отделяет оверлей от залитой вместо фото заглушки.

    Оверлей экспортирован без прозрачности: под ним плавный коричневый фон.
    Нижнее затемнение — чёрный слой, прозрачность которого зависит только от
    строки: считаем его относительно гладкой модели фона. Зерно, пыль и полосы —
    отклонения пикселя от локальной медианы: светлее — белые, темнее — чёрные.
    Возвращает (альфа градиента по строкам, зерно со знаком: >0 белое, <0 чёрное).
    """
    lum = rgb.astype(np.float64).mean(axis=2)
    # медиана 15×15 убирает зерно и тонкие полосы, оставляя фон с градиентом
    smooth = np.array(Image.fromarray(lum.astype(np.uint8)).filter(ImageFilter.MedianFilter(15))).astype(np.float64)
    h, w = lum.shape
    ys, xs = np.mgrid[0:h, 0:w]

    fit = (ys > 0.09 * h) & (ys < 0.62 * h) & (xs > 0.07 * w) & (xs < 0.93 * w)
    coef, *_ = np.linalg.lstsq(_poly_features(xs[fit] / w - 0.5, ys[fit] / h - 0.5, 4), smooth[fit], rcond=None)
    bg = np.maximum((_poly_features(xs.ravel() / w - 0.5, ys.ravel() / h - 0.5, 4) @ coef).reshape(h, w), 1)

    center = slice(int(0.22 * w), int(0.78 * w))
    grad = np.clip(np.median(1 - smooth[:, center] / bg[:, center], axis=1), 0, 1)
    # в строках с шаблонным текстом оценка испорчена — заменяем гладкой кривой
    lower = np.arange(int(0.55 * h), h)
    ok = np.ones(len(lower), dtype=bool)
    if text_rows:
        ok &= (lower < text_rows[0]) | (lower > text_rows[1])
    curve = np.polyval(np.polyfit(lower[ok] / h, grad[lower][ok], 5), lower / h)
    grad[lower[~ok]] = curve[~ok]
    grad = np.convolve(grad, np.ones(9) / 9, mode="same")
    grad[: int(0.55 * h)] = 0
    grad[h - 4 :] = grad[h - 5]  # края свёртки
    grad = np.clip(np.maximum.accumulate(np.where(grad < 0.01, 0, grad)), 0, 1)

    local = np.maximum(smooth, 1)
    dev = lum - local
    grain = np.where(dev > 0, dev / np.maximum(255 - local, 1), dev / local)
    grain[np.abs(grain) < 0.02] = 0
    return grad, grain


def _find_placeholder_text(rgb: np.ndarray) -> tuple[int, int, int, int] | None:
    lum = rgb.astype(np.float64).mean(axis=2)
    h = lum.shape[0]
    mask = lum >= 235
    mask[: h // 2] = False
    # отбрасываем одиночные белые пылинки — текст идёт сплошными строками
    rows = np.nonzero(mask.sum(axis=1) > 20)[0]
    if len(rows) == 0:
        return None
    y0, y1 = rows.min(), rows.max()
    cols = np.nonzero(mask[y0 : y1 + 1].any(axis=0))[0]
    return cols.min(), y0, cols.max(), y1


def prepare_overlay(force: bool = False) -> None:
    """Готовит оверлей с настоящей прозрачностью и без шаблонного текста."""
    fresh = (
        OVERLAY_CLEAN.exists()
        and OVERLAY_META.exists()
        and OVERLAY_CLEAN.stat().st_mtime >= OVERLAY_SRC.stat().st_mtime
    )
    if fresh and not force:
        return

    src = Image.open(OVERLAY_SRC).convert("RGBA")
    rgba = np.array(src)
    h, w = rgba.shape[:2]
    text_box = _find_placeholder_text(rgba[..., :3])

    if rgba[..., 3].min() < 250:
        # оверлей уже прозрачный — только затираем шаблонный текст
        out = rgba.copy()
        if text_box:
            m = int(0.04 * w)
            x0, y0, x1, y1 = text_box
            region = out[max(0, y0 - m) : y1 + m + 1]
            for row in region:
                outside = np.concatenate([row[: max(0, x0 - m)], row[x1 + m + 1 :]])
                row[max(0, x0 - m) : x1 + m + 1] = np.median(outside, axis=0)
    else:
        m = int(0.04 * w)
        text_rows = (text_box[1] - m, text_box[3] + m) if text_box else None
        grad, grain = _remove_placeholder(rgba[..., :3], text_rows)
        if text_box:
            # под шаблонным текстом зерна не видно — копируем его из полосы выше
            x0, y0, x1, y1 = text_box
            ry0, ry1 = max(0, y0 - m), min(h, y1 + m + 1)
            rx0, rx1 = max(0, x0 - m), min(w, x1 + m + 1)
            src0 = max(0, ry0 - (ry1 - ry0))
            grain[ry0:ry1, rx0:rx1] = grain[src0 : src0 + (ry1 - ry0), rx0:rx1]
        # градиент и зерно сводим в один слой: сначала чёрный градиент, поверх — зерно
        white, black = np.clip(grain, 0, 1), np.clip(-grain, 0, 1)
        keep = (1 - grad[:, None]) * (1 - white) * (1 - black)
        alpha = 1 - keep
        color = np.where(alpha > 0, 255 * white / np.maximum(alpha, 1e-6), 0)
        out = np.zeros((h, w, 4), dtype=np.uint8)
        out[..., 0] = out[..., 1] = out[..., 2] = np.clip(color + 0.5, 0, 255)
        out[..., 3] = np.clip(alpha * 255 + 0.5, 0, 255)

    if text_box:
        x0, y0, x1, y1 = text_box
        meta = OverlayMeta(((y0 + y1) / 2) / h, y1 / h, (x1 - x0) / w, (y1 - y0) / h)
    else:
        meta = OverlayMeta(0.81, 0.853, 0.76, 0.086)

    Image.fromarray(out, "RGBA").save(OVERLAY_CLEAN)
    OVERLAY_META.write_text(json.dumps(meta.__dict__, indent=2))


_overlay_cache: dict[tuple[int, int], Image.Image] = {}


def _load_meta() -> OverlayMeta:
    prepare_overlay()
    return OverlayMeta(**json.loads(OVERLAY_META.read_text()))


def _widen(square: Image.Image, width: int) -> Image.Image:
    """Расширяет квадратный оверлей, повторяя его середину.

    Полосы горизонтальные, градиент зависит только от строки, зерно случайное —
    поэтому стыки столбцов не видны, а пыльные края и пятно в углу
    остаются на своих местах без искажений.
    """
    side = square.height
    half = side // 2
    fill_from, fill_to = int(side * 0.2), int(side * 0.7)  # середина без краёв и пятна
    out = Image.new("RGBA", (width, side))
    out.paste(square.crop((0, 0, half, side)), (0, 0))
    x, right = half, width - (side - half)
    while x < right:
        chunk = min(fill_to - fill_from, right - x)
        out.paste(square.crop((fill_from, 0, fill_from + chunk, side)), (x, 0))
        x += chunk
    out.paste(square.crop((half, 0, side, side)), (right, 0))
    return out


def _base_overlay(size: tuple[int, int]) -> Image.Image:
    """Оверлей нужного размера. Для 3:4 квадратный растягивается по высоте
    (полосы и градиент остаются на тех же местах относительно кадра),
    для 4:3 и 16:9 — расширяется повтором середины."""
    if size not in _overlay_cache:
        prepare_overlay()
        w, h = size
        src = Image.open(OVERLAY_CLEAN).convert("RGBA")
        if w <= h:
            _overlay_cache[size] = src.resize(size, Image.LANCZOS)
        else:
            _overlay_cache[size] = _widen(src.resize((h, h), Image.LANCZOS), w)
    return _overlay_cache[size]


# ---------- заголовок ----------

def _glyph(ch: str) -> bytes:
    img = Image.new("L", (80, 80))
    ImageDraw.Draw(img).text((10, 10), ch, font=_probe_font(), fill=255)
    return img.tobytes()


@cache
def _probe_font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), 50)


@cache
def _in_font(ch: str) -> bool:
    # символа нет в шрифте — рисуется так же, как заведомо отсутствующий (пустой прямоугольник);
    # пустой глиф — невидимый служебный символ
    glyph = _glyph(ch)
    return glyph != _glyph("\U0010FFFD") and any(glyph)


# keycap-эмодзи (1️⃣, #️⃣): цифра сама по себе есть в шрифте, убираем её вместе с рамкой
_KEYCAP = re.compile("[0-9#*]\uFE0F?\u20E3")

# селекторы вариантов (❤️ = ❤ + U+FE0F) — сами по себе невидимы
_VARIATION_SELECTOR = re.compile("[\uFE00-\uFE0F\U000E0100-\U000E01EF]")


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


def _segments(line: str) -> list[str]:
    """Строка по кускам: текст отдельно, каждое эмодзи отдельно."""
    return [seg for seg in re.split(f"([{''.join(EMOJI)}])", line) if seg] if EMOJI else [line]


def _emoji_size(font: ImageFont.FreeTypeFont) -> int:
    cap = font.getbbox("Н")[3] - font.getbbox("Н")[1]
    return round(cap * EMOJI_SCALE)


def _line_width(line: str, font: ImageFont.FreeTypeFont) -> float:
    size = _emoji_size(font)
    return sum(size * EMOJI_ADVANCE if seg in EMOJI else font.getlength(seg) for seg in _segments(line))


@cache
def _emoji_image(ch: str, size: int) -> Image.Image:
    return Image.open(EMOJI[ch]).convert("RGBA").resize((size, size), Image.LANCZOS)


def _split_balanced(words: list[str], n: int, font: ImageFont.FreeTypeFont) -> list[str]:
    """Делит слова на n строк так, чтобы самая длинная была минимальной."""
    if n == 1 or len(words) <= 1:
        return [" ".join(words)]
    best: list[str] | None = None
    best_width = float("inf")
    for i in range(1, len(words) - n + 2):
        head = " ".join(words[:i])
        rest = _split_balanced(words[i:], n - 1, font)
        width = max(_line_width(line, font) for line in [head, *rest])
        if width < best_width:
            best, best_width = [head, *rest], width
    return best or [" ".join(words)]


def _layout_title(title: str, max_width: float, base_size: int) -> tuple[list[str], ImageFont.FreeTypeFont]:
    words = title.split()
    min_ok = int(base_size * 0.72)
    candidates = []
    for n in range(1, min(MAX_LINES, len(words)) + 1):
        probe = ImageFont.truetype(str(FONT_PATH), base_size)
        lines = _split_balanced(words, n, probe)
        widest = max(_line_width(line, probe) for line in lines)
        size = min(base_size, int(base_size * max_width / widest))
        # чем больше строк, тем меньше допустимый шрифт
        size = min(size, int(base_size * (1.0, 0.85, 0.7)[n - 1]))
        candidates.append((size, lines))
        if size >= min_ok:
            break
    size, lines = max(candidates, key=lambda c: c[0])
    return lines, ImageFont.truetype(str(FONT_PATH), max(size, 12))


def _draw_title(canvas: Image.Image, title: str, meta: OverlayMeta) -> None:
    """Белый заголовок с мягкой тенью, на месте шаблонного текста."""
    w, h = canvas.size

    probe = ImageFont.truetype(str(FONT_PATH), 100)
    cap = probe.getbbox("Н")[3] - probe.getbbox("Н")[1]
    # размер букв — от короткой стороны, чтобы на всех форматах он был одинаковым
    short = min(w, h)
    base_size = int(100 * meta.cap_height * short / cap)

    max_width = w * 0.88 if w > h else min(w * max(meta.text_width, 0.84), w * 0.88)
    lines, font = _layout_title(title, max_width, base_size)

    line_gap = int(font.size * 0.14)
    cap_h = font.getbbox("Н")[3] - font.getbbox("Н")[1]
    block_h = cap_h * len(lines) + line_gap * (len(lines) - 1)

    # однострочный заголовок — строго на месте шаблона, многострочный растёт вверх;
    # высота букв считается от короткой стороны, положение — от высоты кадра
    bottom = meta.text_bottom * h
    top = min(meta.text_cy * h - cap_h / 2, bottom - block_h)
    top = max(top, h * 0.05)

    shadow = Image.new("L", canvas.size, 0)
    text_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd, td = ImageDraw.Draw(shadow), ImageDraw.Draw(text_layer)
    shadow_dy = font.size * 0.03
    for i, line in enumerate(lines):
        # якорь "ms" — по базовой линии, так высота строки не зависит от букв
        y = top + cap_h + i * (cap_h + line_gap)
        x = (w - _line_width(line, font)) / 2
        for seg in _segments(line):
            if seg in EMOJI:
                # эмодзи — по центру высоты заглавных букв, с той же тенью, что и у текста
                size = _emoji_size(font)
                emoji = _emoji_image(seg, size)
                pos = (round(x + size * (EMOJI_ADVANCE - 1) / 2), round(y - cap_h / 2 - size / 2))
                alpha = emoji.getchannel("A").point(lambda a: a * 210 // 255)
                shadow.paste(alpha, (pos[0], round(pos[1] + shadow_dy)), alpha)
                text_layer.alpha_composite(emoji, pos)
                x += size * EMOJI_ADVANCE
            else:
                sd.text((x, y + shadow_dy), seg, font=font, anchor="ls", fill=210,
                        stroke_width=round(font.size * 0.04))
                td.text((x, y), seg, font=font, anchor="ls", fill=(255, 255, 255, 255))
                x += font.getlength(seg)

    shadow = shadow.filter(ImageFilter.GaussianBlur(font.size * 0.09))
    black = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
    black.putalpha(shadow)
    canvas.alpha_composite(black)
    canvas.alpha_composite(text_layer)


# ---------- публичное API ----------

def render_overlay_layer(size: tuple[int, int], title: str | None) -> Image.Image:
    """Прозрачный слой: оверлей + (если есть) заголовок. Используется и для фото, и для видео."""
    layer = _base_overlay(size).copy()
    if title and (title := clean_title(title)):
        _draw_title(layer, title, _load_meta())
    return layer


def render_overlay_png(size: tuple[int, int], title: str | None) -> bytes:
    out = io.BytesIO()
    render_overlay_layer(size, title).save(out, "PNG")
    return out.getvalue()


def render_post_image(photo: bytes, title: str | None) -> bytes:
    """JPEG: фото, обрезанное по центру в 1:1 или 3:4, с оверлеем и заголовком."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
    size = canvas_size(*img.size)
    img = ImageOps.fit(img, size, Image.LANCZOS, centering=(0.5, 0.5))
    canvas = img.convert("RGBA")
    canvas.alpha_composite(render_overlay_layer(size, title))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=95, subsampling=0)
    return out.getvalue()
