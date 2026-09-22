"""Оформление видео через ffmpeg: кроп в 1:1 или 3:4 + тот же оверлей, что и у фото."""

import asyncio
import json
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from render import canvas_size, render_overlay_png

# Бот может отправить файл до 50 МБ — оставляем запас на контейнер
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
    data: bytes
    width: int
    height: int
    duration: int


@cache
def _h264_encoder() -> str:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    for name in ("libx264", "libopenh264"):
        if f" {name} " in out:
            return name
    raise VideoError("В ffmpeg нет H.264-кодера (нужен libx264 или libopenh264)")


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


async def _probe(path: Path) -> tuple[int, int, float, bool]:
    """Ширина и высота (с учётом поворота), длительность, есть ли звук."""
    info = json.loads(await _run(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ))
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


async def render_video(src: bytes, title: str | None) -> VideoResult:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src_path, overlay_path, out_path = tmp / "src", tmp / "overlay.png", tmp / "out.mp4"
        src_path.write_bytes(src)

        w, h, duration, has_audio = await _probe(src_path)
        cw, ch = canvas_size(w, h)
        overlay_path.write_bytes(await asyncio.to_thread(render_overlay_png, (cw, ch), title))

        budget = MAX_OUTPUT_BYTES * 8 / max(duration, 1) - AUDIO_BITRATE
        bitrate = int(min(MAX_VIDEO_BITRATE, budget))
        if bitrate < 300_000:
            raise VideoError("Видео слишком длинное: в 50 МБ его не уместить в нормальном качестве")

        filters = (
            f"[0:v]scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch},setsar=1[v];"
            f"[v][1:v]overlay=0:0,format=yuv420p[out]"
        )
        args = [
            "ffmpeg", "-y", "-v", "error", "-i", str(src_path), "-i", str(overlay_path),
            "-filter_complex", filters, "-filter_complex_threads", "1", "-map", "[out]",
            "-c:v", _h264_encoder(), "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
            "-threads", FFMPEG_THREADS,
        ]
        if _h264_encoder() == "libx264":
            args += ["-preset", "veryfast", *X264_LOW_MEMORY]
        if has_audio:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", str(AUDIO_BITRATE)]
        args += ["-movflags", "+faststart", str(out_path)]
        await _run(*args)

        data = out_path.read_bytes()
        if len(data) > 50 * 1024 * 1024:
            raise VideoError("После обработки видео больше 50 МБ — Telegram не даст его отправить")
        return VideoResult(data, cw, ch, round(duration))


MUSIC_AUDIO_BITRATE = 192_000
MUSIC_FADE_IN = 0.5
MUSIC_FADE_OUT = 2.0


async def photo_to_music_video(image: bytes, audio: bytes, start: float, duration: float) -> VideoResult:
    """Статичная картинка + отрывок трека [start, start + duration) → mp4."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        img_path, audio_path, out_path = tmp / "img.jpg", tmp / "audio", tmp / "out.mp4"
        img_path.write_bytes(image)
        audio_path.write_bytes(audio)

        info = json.loads(await _run(
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(audio_path)
        ))
        if not any(s["codec_type"] == "audio" for s in info["streams"]):
            raise VideoError("В файле нет звука")
        track_len = float(info["format"].get("duration") or 0)
        if track_len and start >= track_len:
            raise VideoError(f"Трек короче таймкода: его длина {int(track_len // 60)}:{int(track_len % 60):02d}")
        if track_len:
            duration = min(duration, track_len - start)

        w, h, *_ = await _probe(img_path)
        fade_out = min(MUSIC_FADE_OUT, duration / 3)
        args = [
            "ffmpeg", "-y", "-v", "error",
            "-loop", "1", "-framerate", "25", "-i", str(img_path),
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(audio_path),
            "-map", "0:v", "-map", "1:a", "-t", f"{duration:.3f}",
            # размеры кратны 2 — требование yuv420p
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-c:v", _h264_encoder(), "-b:v", "2M", "-threads", FFMPEG_THREADS,
            "-af", f"afade=t=in:d={MUSIC_FADE_IN},afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f}",
            "-c:a", "aac", "-b:a", str(MUSIC_AUDIO_BITRATE),
        ]
        if _h264_encoder() == "libx264":
            args += ["-preset", "veryfast", "-tune", "stillimage", *X264_LOW_MEMORY]
        args += ["-movflags", "+faststart", str(out_path)]
        await _run(*args)

        data = out_path.read_bytes()
        if len(data) > 50 * 1024 * 1024:
            raise VideoError("Видео получилось больше 50 МБ — возьми отрывок короче")
        return VideoResult(data, w - w % 2, h - h % 2, round(duration))
