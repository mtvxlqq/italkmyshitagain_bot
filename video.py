"""Оформление видео через ffmpeg: кадр в окне шаблона (тот же слой, что и у фото) или просто кроп.

Исходники и результат — файлы на диске: видео до 50 МБ не держим в памяти целиком.
"""

import asyncio
import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from render import Layout, canvas_size, layout_png

UPLOAD_LIMIT = 50 * 1024 * 1024  # больше бот отправить не может
# итоговое видео целимся уложить сюда — запас на контейнер
MAX_OUTPUT_BYTES = 48 * 1024 * 1024
MAX_VIDEO_BITRATE = 6_000_000
AUDIO_BITRATE = 128_000
# На хостинге мало памяти, а в контейнере ffmpeg видит все ядра хоста и заводит
# буферы кадров на каждый поток — без ограничения его убивает OOM
FFMPEG_THREADS = "2"
X264_LOW_MEMORY = ["-x264-params", "rc-lookahead=10:sync-lookahead=0"]


class VideoError(Exception):
    pass


@dataclass
class VideoResult:
    path: Path
    width: int
    height: int
    duration: int


@cache
def _h264_encoder() -> str:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    # строки вида « V....D libx264   описание»: имя кодера — второе слово
    names = {parts[1] for line in out.splitlines() if len(parts := line.split()) > 1}
    for name in ("libx264", "libopenh264"):
        if name in names:
            return name
    raise VideoError("В ffmpeg нет H.264-кодера (нужен libx264 или libopenh264)")


async def _encoder_args(*x264_tune: str) -> list[str]:
    """Кодер H.264 и его настройки: у libx264 — быстрый пресет и экономия памяти."""
    encoder = await asyncio.to_thread(_h264_encoder)  # первый раз спрашивает ffmpeg — не держим цикл
    args = ["-c:v", encoder, "-threads", FFMPEG_THREADS]
    if encoder == "libx264":
        args += ["-preset", "veryfast", *x264_tune, *X264_LOW_MEMORY]
    return args


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        if not tail:
            # ничего не написал — скорее всего, процесс убит сигналом (-9 — нехватка памяти)
            tail = [f"процесс завершился с кодом {proc.returncode}"
                    + (" (не хватило памяти)" if proc.returncode == -9 else "")]
        raise VideoError(f"{Path(args[0]).name}: " + " | ".join(tail))
    return stdout.decode()


async def _ffprobe(path: Path) -> dict:
    return json.loads(await _run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))


async def _probe(path: Path) -> tuple[int, int, float, bool]:
    """Ширина и высота (с учётом поворота), длительность, есть ли звук."""
    info = await _ffprobe(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise VideoError("В файле нет видеодорожки")
    w, h = int(video["width"]), int(video["height"])
    rotation = int(video.get("tags", {}).get("rotate", 0))
    for side in video.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation))
    if abs(rotation) % 180 == 90:
        w, h = h, w
    duration = float(info["format"].get("duration") or video.get("duration") or 0)
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    return w, h, duration, has_audio


def _check_size(out: Path, error: str) -> None:
    if out.stat().st_size > UPLOAD_LIMIT:
        raise VideoError(error)


async def render_video(src: Path, out: Path, make_layout: Callable[[tuple[int, int]], Layout] | None,
                       size: tuple[int, int] | None = None) -> VideoResult:
    """Видео в шаблоне: кадр встаёт в окно layout.rect, сверху — слой шаблона.
    make_layout строит слой по размеру кадра; None — без оформления: кадр целиком
    (слишком высокий или широкий — с размытыми полями) и перекодирование.
    size — точный размер видео без оформления (слайд карусели); по умолчанию — по кадру.
    Результат пишется в out."""
    w, h, duration, has_audio = await _probe(src)
    layout = await asyncio.to_thread(make_layout, (w, h)) if make_layout else None
    if layout:
        (cw, ch), (x, y, rw, rh) = layout.size, layout.rect
    else:
        cw, ch = size or canvas_size(w, h)
        x, y, rw, rh = 0, 0, cw, ch

    budget = MAX_OUTPUT_BYTES * 8 / max(duration, 1) - AUDIO_BITRATE
    bitrate = int(min(MAX_VIDEO_BITRATE, budget))
    if bitrate < 300_000:
        raise VideoError("Видео слишком длинное: в 50 МБ его не уместить в нормальном качестве")

    if layout is None or layout.whole:
        # кадр целиком, поля — размытая и затемнённая копия (размываем уменьшенную — так дешевле)
        filters = (
            f"[0:v]split[a][b];"
            f"[a]scale={rw // 8}:{rh // 8}:force_original_aspect_ratio=increase,crop={rw // 8}:{rh // 8},"
            f"gblur=sigma=4,eq=brightness=-0.25,scale={rw}:{rh}[bg];"
            f"[b]scale={rw}:{rh}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    else:
        filters = f"[0:v]scale={rw}:{rh}:force_original_aspect_ratio=increase,crop={rw}:{rh},setsar=1"
    if layout:
        if layout.gray:
            filters += ",hue=s=0"
        filters += f",pad={cw}:{ch}:{x}:{y}:black[v];[v][1:v]overlay=0:0"
    filters += ",format=yuv420p[out]"

    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "overlay.png"
        if layout:
            overlay.write_bytes(await asyncio.to_thread(layout_png, layout))
        inputs = ["-i", str(src)] + (["-i", str(overlay)] if layout else [])
        args = [
            "ffmpeg", "-y", "-v", "error", *inputs,
            "-filter_complex", filters, "-filter_complex_threads", "1", "-map", "[out]",
            *await _encoder_args(),
            "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
        ]
        if has_audio:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", str(AUDIO_BITRATE)]
        await _run(*args, "-movflags", "+faststart", str(out))

    _check_size(out, "После обработки видео больше 50 МБ — Telegram не даст его отправить")
    return VideoResult(out, cw, ch, round(duration))


MUSIC_AUDIO_BITRATE = 192_000
MUSIC_FADE_IN = 0.5
MUSIC_FADE_OUT = 2.0


async def photo_to_music_video(image: bytes, audio: Path, out: Path, start: float, duration: float) -> VideoResult:
    """Статичная картинка + отрывок трека [start, start + duration) → mp4 в out."""
    info = await _ffprobe(audio)
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise VideoError("В файле нет звука")
    track_len = float(info["format"].get("duration") or 0)
    if track_len and start >= track_len:
        raise VideoError(f"Трек короче таймкода: его длина {int(track_len // 60)}:{int(track_len % 60):02d}")
    if track_len:
        duration = min(duration, track_len - start)

    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "img.jpg"
        img.write_bytes(image)
        w, h, *_ = await _probe(img)
        fade_out = min(MUSIC_FADE_OUT, duration / 3)
        await _run(
            "ffmpeg", "-y", "-v", "error",
            "-loop", "1", "-framerate", "25", "-i", str(img),
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(audio),
            "-map", "0:v", "-map", "1:a", "-t", f"{duration:.3f}",
            # размеры кратны 2 — требование yuv420p
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            *await _encoder_args("-tune", "stillimage"), "-b:v", "2M",
            "-af", f"afade=t=in:d={MUSIC_FADE_IN},afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f}",
            "-c:a", "aac", "-b:a", str(MUSIC_AUDIO_BITRATE),
            "-movflags", "+faststart", str(out),
        )

    _check_size(out, "Видео получилось больше 50 МБ — возьми отрывок короче")
    return VideoResult(out, w - w % 2, h - h % 2, round(duration))
