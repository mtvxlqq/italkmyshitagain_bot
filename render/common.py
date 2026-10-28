"""Общее для всех картинок: ассеты, цвета из макетов, открытие и сохранение, кадр без обрезки, маски."""

import io
import math
from functools import cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

ASSETS = Path(__file__).parent.parent / "assets"

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

ImageSource = bytes | Path  # картинка в памяти или скачанный файл

# ---------- кадр без обрезки ----------

SIDE = 1080  # короткая сторона результата
# Telegram в ленте обрезает по центру всё, что выше 4:5, — выше этого не делаем,
# иначе не видно ни шапки, ни заголовка, пока не откроешь фото
MAX_TALL = 5 / 4   # высота / ширина
MAX_WIDE = 16 / 9  # ширина / высота


def even(x: float) -> int:
    return 2 * round(x / 2)  # yuv420p в видео требует чётных сторон


def canvas_size(width: int, height: int) -> tuple[int, int]:
    """Размер результата без оверлея: пропорции кадра, но не выше 4:5 и не шире 16:9."""
    if height > width:
        return SIDE, even(SIDE * min(height / width, MAX_TALL))
    return even(SIDE * min(width / height, MAX_WIDE)), SIDE


def open_image(photo: ImageSource) -> Image.Image:
    src = photo if isinstance(photo, Path) else io.BytesIO(photo)
    return ImageOps.exif_transpose(Image.open(src)).convert("RGB")


def image_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def to_jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=95, subsampling=0)
    return out.getvalue()


def fit_whole(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Фото целиком в рамке size; пустые поля — размытая и затемнённая копия кадра."""
    if abs(math.log(img.width / img.height * size[1] / size[0])) < 0.02:
        return ImageOps.fit(img, size, Image.LANCZOS)  # разница в пару пикселей — полей не нужно
    small = ImageOps.fit(img, (max(size[0] // 8, 1), max(size[1] // 8, 1)), Image.LANCZOS)
    bg = small.filter(ImageFilter.GaussianBlur(4)).resize(size, Image.LANCZOS)
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    fg = ImageOps.contain(img, size, Image.LANCZOS)
    bg.paste(fg, ((size[0] - fg.width) // 2, (size[1] - fg.height) // 2))
    return bg


# ---------- маски ----------

def to_alpha(mask: np.ndarray) -> np.ndarray:
    """Маска 0…1 → канал прозрачности 0…255."""
    return (mask * 255).clip(0, 255).round().astype(np.uint8)


def _supersampled_mask(size: tuple[int, int], box: tuple[int, int, int, int], radius: int) -> np.ndarray:
    k = 4  # рисуем вчетверо крупнее и уменьшаем — так край сглажен
    big = Image.new("L", (size[0] * k, size[1] * k))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(big).rounded_rectangle((x0 * k, y0 * k, x1 * k - 1, y1 * k - 1), radius * k, fill=255)
    return np.asarray(big.resize(size, Image.BOX), dtype=np.float32) / 255


@cache
def _corners(radius: int) -> np.ndarray:
    """Маска скруглённого квадрата чуть больше двух радиусов: из неё берём сглаженные углы."""
    side = 2 * radius + 6
    mask = _supersampled_mask((side, side), (0, 0, side, side), radius)
    mask.flags.writeable = False
    return mask


def rounded_mask(size: tuple[int, int], box: tuple[int, int, int, int], radius: int) -> np.ndarray:
    """Маска 0…1: 1 внутри скруглённого прямоугольника (сглаженные углы)."""
    x0, y0, x1, y1 = box
    c = radius + 2  # сглаживание занимает только квадраты radius × radius в углах
    if not (0 <= x0 and 0 <= y0 and x1 <= size[0] and y1 <= size[1]
            and x1 - x0 > 2 * c + 1 and y1 - y0 > 2 * c + 1):
        return _supersampled_mask(size, box, radius)
    # прямые края попадают в пиксели целиком — сглаживать нужно только углы,
    # а их берём из маленькой маски того же радиуса (результат тот же, что у большой)
    mask = np.zeros((size[1], size[0]), dtype=np.float32)
    mask[y0:y1, x0:x1] = 1
    corners = _corners(radius)
    mask[y0:y0 + c, x0:x0 + c] = corners[:c, :c]
    mask[y0:y0 + c, x1 - c:x1] = corners[:c, -c:]
    mask[y1 - c:y1, x0:x0 + c] = corners[-c:, :c]
    mask[y1 - c:y1, x1 - c:x1] = corners[-c:, -c:]
    return mask


@cache
def circle(size: int) -> Image.Image:
    """Сглаженный круг — маска для аватарок."""
    return Image.fromarray(to_alpha(rounded_mask((size, size), (0, 0, size, size), size // 2)))
