"""Бот канала italkmyshitagain: оформление и публикация постов, отложка, подписчики, статистика."""

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
)

from caption import build_caption, caption_too_long_for_message, parse_caption
from config import Config, load_config
from db import Database, Post
from render import (
    MODES, RELEASES_MAX, Card, card_layout, clean_title, looks_like_quote, make_card, render_card,
    render_plain, set_brand,
)
from video import VideoError, photo_to_music_video, render_video

log = logging.getLogger("bot")

SCHEDULER_INTERVAL = 15  # секунд между проверками очереди
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
ALBUM_WAIT = 1.5  # сек: части альбома приходят отдельными сообщениями, ждём все
DOWNLOAD_LIMIT = 20 * 1024 * 1024  # больше Bot API скачать не даёт

HELP = (
    "<b>Как сделать пост</b>\n"
    "Пришли фото или видео (или альбом до 10 штук) с подписью:\n"
    "• <b>первая строка</b> — заголовок: попадёт на карточку и останется "
    "первой строкой поста жирным;\n"
    "• всё остальное — текст поста, оставлю как есть (с форматированием).\n\n"
    "<b>Оформление</b> — кнопками под превью:\n"
    "• 📰 <b>Новость</b> — фото в рамке, заголовок снизу (по умолчанию);\n"
    "• 📣 <b>Анонс</b> — фото слева, карточка «?», заголовок по центру;\n"
    "• 💿 <b>Релизы</b> — «Эти релизы вышли сегодня» и до трёх обложек из альбома;\n"
    "• 💬 <b>Цитата</b> — затемнённое чёрно-белое фото и текст в «ёлочках» "
    "(включается само, если первая строка в кавычках или оформлена цитатой);\n"
    "• 🖼 <b>Без оверлея</b> — только обрезка под формат.\n\n"
    "Слова заголовка, выделенные <i>курсивом</i> или <u>подчёркиванием</u>, будут красными. "
    "Без выделения красным станет конец заголовка. В анонсе и цитате хвост "
    "<code>— Имя</code> уйдёт в подпись под заголовком (артист / автор).\n\n"
    "Карточка — только первый файл, остальные файлы альбома идут без оформления. "
    "В конец текста добавлю жирную ссылку на канал. "
    "Дальше — опубликовать сразу или отложить.\n\n"
    "Видео — до 20 МБ (ограничение Telegram для ботов).\n\n"
    "<b>Музыка</b>\n"
    "Пришли трек после поста — спрошу таймкод и сделаю из карточки видео с этим отрывком.\n\n"
    "<b>Команды</b>\n"
    "/queue — отложенные посты\n"
    "/stats — статистика канала за сегодня\n"
    "/cancel — отменить ввод времени"
)

SCHEDULE_PROMPT = (
    "Когда опубликовать пост #{id}? Напиши время, например:\n"
    "• <code>18:30</code> — сегодня (или завтра, если уже прошло)\n"
    "• <code>завтра 10:00</code>\n"
    "• <code>25.09 19:00</code> или <code>25.09.2026 19:00</code>\n"
    "• <code>+2ч</code>, <code>+30м</code>\n\n"
    "Часовой пояс: {tz}. Или выбери вариант ниже."
)


@dataclass
class App:
    bot: Bot
    cfg: Config
    db: Database
    channel_id: int | None = None
    channel_username: str | None = None
    # рендер видео тяжёлый — обрабатываем посты по одному
    render_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    def fmt(self, dt: datetime) -> str:
        return dt.astimezone(self.cfg.tz).strftime("%d.%m.%Y %H:%M")

    async def notify_admins(self, text: str, **kwargs) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, **kwargs)
            except Exception:
                log.exception("Не удалось отправить сообщение админу %s", admin_id)


class PostCB(CallbackData, prefix="post"):
    action: str  # publish | schedule | discard | unschedule | now | quick | mode
    id: int
    arg: str = ""


class ScheduleForm(StatesGroup):
    waiting_time = State()


class MusicForm(StatesGroup):
    waiting_timecode = State()


MUSIC_DEFAULT_LEN = 30   # сек, если указано только начало
MUSIC_MAX_LEN = 300
_TC_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$|^(\d+)$")


def _seconds(text: str) -> int | None:
    m = _TC_RE.match(text.strip())
    if not m:
        return None
    if m[4]:
        return int(m[4])
    return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])


def parse_timecode(text: str) -> tuple[int, int] | None:
    """«1:05» → (65, 30); «1:05-1:40» → (65, 35). None — формат не понят."""
    parts = re.split(r"\s*[-–—]\s*", text.strip())
    if len(parts) > 2:
        return None
    start = _seconds(parts[0])
    if start is None:
        return None
    if len(parts) == 1:
        return start, MUSIC_DEFAULT_LEN
    end = _seconds(parts[1])
    if end is None or end <= start:
        return None
    return start, min(end - start, MUSIC_MAX_LEN)


# ---------- время ----------

_REL_RE = re.compile(r"^\+\s*(\d+)\s*(м|мин|минут\w*|m|min|ч|час\w*|h)$")
_TIME_RE = re.compile(r"^(?:(сегодня|завтра|послезавтра)\s+)?(\d{1,2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?\s+(\d{1,2}):(\d{2})$")
_DAY_SHIFT = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def parse_when(text: str, now: datetime) -> datetime | None:
    """Разбирает время публикации. Возвращает None, если формат непонятен."""
    text = text.strip().lower()
    try:
        if m := _REL_RE.match(text):
            amount = int(m[1])
            minutes = amount if m[2][0] in "мm" else amount * 60
            return now + timedelta(minutes=minutes)

        if m := _TIME_RE.match(text):
            day = now + timedelta(days=_DAY_SHIFT.get(m[1] or "сегодня", 0))
            when = day.replace(hour=int(m[2]), minute=int(m[3]), second=0, microsecond=0)
            if not m[1] and when <= now:
                when += timedelta(days=1)
            return when

        if m := _DATE_RE.match(text):
            year = int(m[3]) if m[3] else now.year
            if year < 100:
                year += 2000
            when = now.replace(
                year=year, month=int(m[2]), day=int(m[1]),
                hour=int(m[4]), minute=int(m[5]), second=0, microsecond=0,
            )
            if not m[3] and when <= now:
                when = when.replace(year=year + 1)
            return when
    except ValueError:  # 31.02, 25:00 и т. п.
        return None
    return None


def quick_time(code: str, now: datetime) -> datetime:
    if code.startswith("h"):
        return now + timedelta(hours=int(code[1:]))
    # "t10" — завтра в 10:00
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=int(code[1:]), minute=0, second=0, microsecond=0)


# ---------- клавиатуры ----------

def draft_kb(post_id: int, mode: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки черновика; mode — текущее оформление (None у постов до режимов — без переключателя)."""
    modes = []
    if mode:
        buttons = [
            InlineKeyboardButton(
                text=("✓ " if key == mode else "") + label,
                callback_data=PostCB(action="mode", id=post_id, arg=key).pack(),
            )
            for key, label in MODES.items()
        ]
        modes = [buttons[:3], buttons[3:]]
    return InlineKeyboardMarkup(inline_keyboard=[
        *modes,
        [
            InlineKeyboardButton(text="🚀 Опубликовать", callback_data=PostCB(action="publish", id=post_id).pack()),
            InlineKeyboardButton(text="⏰ Отложить", callback_data=PostCB(action="schedule", id=post_id).pack()),
        ],
        [InlineKeyboardButton(text="✖️ Удалить черновик", callback_data=PostCB(action="discard", id=post_id).pack())],
    ])


def quick_kb(post_id: int) -> InlineKeyboardMarkup:
    def btn(text: str, code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=PostCB(action="quick", id=post_id, arg=code).pack())

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Через 1 час", "h1"), btn("Через 3 часа", "h3")],
        [btn("Завтра 10:00", "t10"), btn("Завтра 19:00", "t19")],
    ])


def scheduled_kb(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Сейчас", callback_data=PostCB(action="now", id=post_id).pack()),
        InlineKeyboardButton(text="❌ Отменить", callback_data=PostCB(action="unschedule", id=post_id).pack()),
    ]])


# ---------- публикация ----------

async def send_media(bot: Bot, chat_id: int | str, items: list[dict], caption: str | None) -> list[Message]:
    """Отправляет одно медиа или альбом. item: type, file (file_id или InputFile), для видео — размеры."""
    def video_kwargs(item: dict) -> dict:
        return {k: item[k] for k in ("width", "height", "duration") if item.get(k)}

    if len(items) == 1:
        item = items[0]
        if item["type"] == "video":
            return [await bot.send_video(
                chat_id, item["file"], caption=caption, supports_streaming=True, **video_kwargs(item)
            )]
        return [await bot.send_photo(chat_id, item["file"], caption=caption)]

    group = []
    for i, item in enumerate(items):
        extra = {"caption": caption} if i == 0 and caption else {}
        if item["type"] == "video":
            group.append(InputMediaVideo(media=item["file"], supports_streaming=True, **video_kwargs(item), **extra))
        else:
            group.append(InputMediaPhoto(media=item["file"], **extra))
    return await bot.send_media_group(chat_id, group)


async def publish(app: App, post: Post) -> str:
    """Отправляет пост в канал, возвращает ссылку на него (или название канала)."""
    target = app.channel_id or app.cfg.channel
    items = [dict(m, file=m["file_id"]) for m in post.media]
    sent = await send_media(app.bot, target, items, None if post.split_text else post.caption_html)
    if post.split_text:
        await app.bot.send_message(target, post.caption_html, link_preview_options=NO_PREVIEW)
    app.db.set_status(post.id, "published")
    if app.channel_username:
        return f"https://t.me/{app.channel_username}/{sent[0].message_id}"
    return app.cfg.channel


async def publish_claimed(app: App, post_id: int, allowed: tuple[str, ...]) -> str | None:
    """Публикует пост, если он ещё в одном из статусов allowed. None — уже обработан кем-то другим."""
    if not app.db.claim_for_publishing(post_id, allowed):
        return None
    post = app.db.get_post(post_id)
    try:
        return await publish(app, post)
    except Exception:
        # возвращаем прежний статус, чтобы можно было повторить
        app.db.set_status(post_id, allowed[0])
        raise


async def scheduler_loop(app: App) -> None:
    while True:
        try:
            for post in app.db.due_posts():
                if not app.db.claim_for_publishing(post.id, ("scheduled",)):
                    continue
                try:
                    url = await publish(app, post)
                    await app.notify_admins(
                        f"✅ Отложенный пост #{post.id} «{html.escape(post.title)}» опубликован: {url}",
                        link_preview_options=NO_PREVIEW,
                    )
                except Exception as e:
                    log.exception("Ошибка публикации поста %s", post.id)
                    app.db.set_status(post.id, "failed")
                    await app.notify_admins(
                        f"⚠️ Не удалось опубликовать отложенный пост #{post.id}: <code>{html.escape(str(e))}</code>"
                    )
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(SCHEDULER_INTERVAL)


# ---------- статистика ----------

async def build_stats(app: App, day_start: datetime, day_end: datetime) -> tuple[str, int]:
    count = await app.bot.get_chat_member_count(app.channel_id or app.cfg.channel)
    events = app.db.member_events_between(day_start, day_end)
    joined, left = events.get("join", 0), events.get("leave", 0)

    prev = app.db.daily_count((day_start - timedelta(days=1)).strftime("%Y-%m-%d"))
    delta = count - prev if prev is not None else joined - left
    sign = "+" if delta >= 0 else ""

    posts = app.db.published_between(day_start, day_end)
    queued = len(app.db.scheduled_posts())
    text = (
        f"📊 <b>Статистика канала за {day_start.strftime('%d.%m.%Y')}</b>\n\n"
        f"👥 Подписчиков: <b>{count}</b> ({sign}{delta} за день)\n"
        f"➕ Подписались: {joined}\n"
        f"➖ Отписались: {left}\n"
        f"📝 Опубликовано постов: {posts}\n"
        f"⏰ В очереди: {queued}"
    )
    return text, count


def today_bounds(app: App) -> tuple[datetime, datetime]:
    now = app.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


async def daily_stats_loop(app: App) -> None:
    while True:
        now = app.now()
        run_at = now.replace(hour=app.cfg.stats_time.hour, minute=app.cfg.stats_time.minute, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        await asyncio.sleep((run_at - now).total_seconds())
        try:
            start, end = today_bounds(app)
            text, count = await build_stats(app, start, end)
            app.db.save_daily_count(start.strftime("%Y-%m-%d"), count)
            if count >= app.cfg.subscribers_threshold:
                await app.notify_admins(text)
        except Exception:
            log.exception("Ошибка ежедневной статистики")
        await asyncio.sleep(60)  # чтобы не сработать дважды в ту же минуту


# ---------- админские хендлеры ----------

admin = Router(name="admin")


@admin.message(CommandStart())
@admin.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP)


@admin.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.")


@admin.message(Command("stats"))
async def cmd_stats(message: Message, app: App) -> None:
    start, end = today_bounds(app)
    text, _ = await build_stats(app, start, end)
    await message.answer(text)


@admin.message(Command("queue"))
async def cmd_queue(message: Message, app: App) -> None:
    posts = app.db.scheduled_posts()
    if not posts:
        await message.answer("Очередь пуста.")
        return
    await message.answer(f"Отложенных постов: {len(posts)}")
    for post in posts:
        first = post.media[0]
        album = f" (альбом, {len(post.media)} шт.)" if len(post.media) > 1 else ""
        text = f"#{post.id} «{html.escape(post.title)}»{album}\n⏰ {app.fmt(post.publish_at)}"
        send = message.answer_video if first["type"] == "video" else message.answer_photo
        await send(first["file_id"], caption=text, reply_markup=scheduled_kb(post.id))


# ---------- посты ----------

MEDIA = F.photo | F.video | (
    F.document & (F.document.mime_type.startswith("image/") | F.document.mime_type.startswith("video/"))
)
DEFAULT_MODE = "news"

_albums: dict[str, list[Message]] = {}
_album_tasks: dict[str, asyncio.Task] = {}


class PostError(Exception):
    """Ошибка оформления, которую можно показать как есть."""


@admin.message(MEDIA)
async def on_media(message: Message, app: App, state: FSMContext) -> None:
    await state.clear()
    group_id = message.media_group_id
    if not group_id:
        await process_post(app, [message])
        return
    # части альбома приходят отдельными апдейтами — собираем, пока не перестанут приходить
    _albums.setdefault(group_id, []).append(message)
    if task := _album_tasks.get(group_id):
        task.cancel()
    _album_tasks[group_id] = asyncio.create_task(_finish_album(app, group_id))


async def _finish_album(app: App, group_id: str) -> None:
    await asyncio.sleep(ALBUM_WAIT)
    _album_tasks.pop(group_id, None)
    messages = sorted(_albums.pop(group_id, []), key=lambda m: m.message_id)
    try:
        await process_post(app, messages)
    except Exception as e:
        log.exception("Ошибка оформления альбома")
        await messages[0].answer(f"⚠️ Не получилось оформить альбом: <code>{html.escape(str(e))}</code>")


def _source(message: Message) -> tuple[str, str, int]:
    """Тип ("photo"/"video"), file_id и размер исходного файла."""
    if message.photo:
        p = message.photo[-1]
        return "photo", p.file_id, p.file_size or 0
    if message.video:
        return "video", message.video.file_id, message.video.file_size or 0
    doc = message.document
    kind = "video" if (doc.mime_type or "").startswith("video/") else "photo"
    return kind, doc.file_id, doc.file_size or 0


def _stored(sent: Message, item: dict) -> dict:
    """Что сохранить о медиа из превью, чтобы потом опубликовать его без повторной загрузки."""
    if item["type"] == "video":
        return {"type": "video", "file_id": sent.video.file_id,
                **{k: item[k] for k in ("width", "height", "duration")}}
    return {"type": "photo", "file_id": sent.photo[-1].file_id}


def post_style(post: Post) -> dict | None:
    """Как оформлен черновик: режим, исходные файлы и выделение в заголовке.

    Лежит в первом медиа; у постов, сделанных до появления режимов, — None.
    """
    return post.media[0].get("style")


def _cover_indexes(sources: list[dict]) -> list[int]:
    return [i for i, s in enumerate(sources) if s["kind"] == "photo"][:RELEASES_MAX]


async def render_media(app: App, chat_id: int, sources: list[dict], mode: str, card: Card) -> list[dict]:
    """Оформляет исходники для send_media.

    Первый файл — карточка шаблона, остальные — без оверлея. В релизах первые фото
    (до трёх) склеиваются в одну карточку с обложками.
    """
    async def download(file_id: str) -> bytes:
        return (await app.bot.download(file_id)).read()

    has_video = any(s["kind"] == "video" for s in sources)
    items: list[dict] = []

    def add_photo(image: bytes) -> None:
        items.append({"type": "photo", "file": BufferedInputFile(image, f"{len(items)}.jpg")})

    async with app.render_lock:
        await app.bot.send_chat_action(chat_id, "upload_video" if has_video else "upload_photo")
        rest = list(enumerate(sources))
        if mode == "releases":
            covers = _cover_indexes(sources)
            if not covers:
                raise PostError("Для релизов нужны фото обложек (до трёх).")
            photos = [await download(sources[i]["file_id"]) for i in covers]
            add_photo(await asyncio.to_thread(render_card, mode, card, photos))
            rest = [(i, s) for i, s in rest if i not in covers]

        for n, (_, src) in enumerate(rest):
            await app.bot.send_chat_action(chat_id, "upload_video" if has_video else "upload_photo")
            data = await download(src["file_id"])
            templated = n == 0 and mode not in ("releases", "plain")
            if src["kind"] == "photo":
                if templated:
                    add_photo(await asyncio.to_thread(render_card, mode, card, [data]))
                else:
                    add_photo(await asyncio.to_thread(render_plain, data))
            else:
                layout = await asyncio.to_thread(card_layout, mode, card) if templated else None
                video = await render_video(data, layout)
                items.append({
                    "type": "video", "file": BufferedInputFile(video.data, f"{len(items)}.mp4"),
                    "width": video.width, "height": video.height, "duration": video.duration,
                })
    return items


async def make_preview(app: App, chat_id: int, sources: list[dict], mode: str, title: str,
                       accent: list, caption: str, split: bool) -> list[dict] | None:
    """Оформляет пост, присылает превью и возвращает медиа для черновика.

    None — не получилось; причину уже написали в чат.
    """
    has_video = any(s["kind"] == "video" for s in sources)
    status = None
    if has_video or len(sources) > 1:
        status = await app.bot.send_message(
            chat_id, "⏳ Оформляю…" + (" Видео обрабатывается дольше фото." if has_video else "")
        )
    try:
        rendered = await render_media(app, chat_id, sources, mode, make_card(title, accent, mode))
    except (PostError, VideoError) as e:
        await app.bot.send_message(chat_id, f"⚠️ {html.escape(str(e))}")
        return None
    finally:
        if status:
            await status.delete()

    sent = await send_media(app.bot, chat_id, rendered, None if split else caption)
    if split:
        await app.bot.send_message(chat_id, caption, link_preview_options=NO_PREVIEW)
    media = [_stored(m, item) for m, item in zip(sent, rendered)]
    media[0]["style"] = {"mode": mode, "accent": accent, "sources": sources}
    return media


def draft_text(post_id: int, mode: str | None, split: bool, extra: str = "") -> str:
    head = f"👆 Превью поста #{post_id}"
    if mode:
        head += f" — {MODES[mode]}{extra}. Другое оформление — кнопками ниже"
    text = head + ". Что делаем?"
    if split:
        text += ("\n\n⚠️ Текст длиннее 1024 символов — в подпись не влезет, "
                 "поэтому опубликую его отдельным сообщением сразу под постом.")
    return text


async def process_post(app: App, messages: list[Message]) -> None:
    first = messages[0]
    caption_msg = next((m for m in messages if m.caption), None)
    if not caption_msg:
        await first.answer("Добавь подпись: первая строка — заголовок, дальше — текст поста.")
        return

    parsed = parse_caption(caption_msg.caption, caption_msg.caption_entities)
    if not clean_title(parsed.title):
        await first.answer("Не нашёл заголовок — первая строка подписи должна содержать текст.")
        return
    if caption_too_long_for_message(parsed, app.cfg.channel_link_text):
        await first.answer("Текст слишком длинный для Telegram (больше 4096 символов).")
        return

    sources = [_source(m) for m in messages]
    if any(size > DOWNLOAD_LIMIT for _, _, size in sources):
        await first.answer(
            "Файл больше 20 МБ — Telegram не даёт ботам скачивать такие. Сожми видео и пришли снова."
        )
        return

    mode = "quote" if parsed.blockquote or looks_like_quote(parsed.title) else DEFAULT_MODE
    caption, split = build_caption(parsed, app.cfg.channel_link_text, app.cfg.channel_url)
    media = await make_preview(
        app, first.chat.id, [{"kind": k, "file_id": f} for k, f, _ in sources],
        mode, parsed.title, parsed.accent, caption, split,
    )
    if media is None:
        return
    post_id = app.db.add_draft(media, caption, split, parsed.title)
    await first.answer(draft_text(post_id, mode, split), reply_markup=draft_kb(post_id, mode))


@admin.callback_query(PostCB.filter(F.action == "mode"))
async def cb_mode(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    post = app.db.get_post(callback_data.id)
    if not post or post.status != "draft":
        await query.answer("Этот пост уже обработан.", show_alert=True)
        return
    style = post_style(post)
    if not style:
        await query.answer("Пост сделан до появления режимов — пришли его заново.", show_alert=True)
        return
    mode = callback_data.arg
    if style["mode"] == mode:
        await query.answer("Уже в этом оформлении")
        return
    await query.answer()
    # убираем кнопки, чтобы не нажать дважды, пока рендерится
    await query.message.edit_text(f"⏳ Пост #{post.id}: меняю оформление на {MODES[mode]}…")

    media = await make_preview(
        app, query.message.chat.id, style["sources"], mode, post.title, style["accent"],
        post.caption_html, post.split_text,
    )
    if media is None:
        await query.message.edit_text(draft_text(post.id, style["mode"], post.split_text),
                                      reply_markup=draft_kb(post.id, style["mode"]))
        return
    app.db.update_media(post.id, media)
    await query.message.edit_text(f"Пост #{post.id}: оформление сменилось на {MODES[mode]}, новое превью ниже.")
    await query.message.answer(draft_text(post.id, mode, post.split_text), reply_markup=draft_kb(post.id, mode))


AUDIO = F.audio | (F.document & F.document.mime_type.startswith("audio/"))


@admin.message(AUDIO)
async def on_audio(message: Message, app: App, state: FSMContext) -> None:
    post = app.db.latest_draft()
    if not post:
        await message.answer("Сначала пришли пост с фото — музыку я наложу на его последний черновик.")
        return
    style = post_style(post)
    photo_first = (
        (style["mode"] == "releases" or style["sources"][0]["kind"] == "photo") if style
        else post.media[0]["type"] == "photo"
    )
    if not photo_first:
        await message.answer(f"Пост #{post.id} начинается с видео — музыку накладываю только на фото.")
        return
    audio = message.audio or message.document
    if (audio.file_size or 0) > DOWNLOAD_LIMIT:
        await message.answer("Трек больше 20 МБ — Telegram не даёт ботам скачивать такие файлы.")
        return

    await state.set_state(MusicForm.waiting_timecode)
    await state.update_data(post_id=post.id, audio_id=audio.file_id)
    length = ""
    if message.audio and message.audio.duration:
        d = message.audio.duration
        length = f" (длина трека {d // 60}:{d % 60:02d})"
    await message.answer(
        f"🎵 Сделаю из поста #{post.id} «{html.escape(post.title)}» видео с этим треком{length}.\n\n"
        f"С какого момента? Пришли таймкод:\n"
        f"• <code>1:05</code> — {MUSIC_DEFAULT_LEN} секунд с 1:05\n"
        f"• <code>1:05-1:40</code> — конкретный отрывок\n\n"
        f"Или /cancel."
    )


async def _first_image(app: App, post: Post) -> bytes:
    """Первая картинка поста в полном качестве: перерисовываем из исходников, а не берём сжатое превью."""
    style = post_style(post)
    if not style:  # пост до появления режимов — берём готовую картинку
        return (await app.bot.download(post.media[0]["file_id"])).read()
    mode, sources = style["mode"], style["sources"]
    picked = [sources[i] for i in _cover_indexes(sources)] if mode == "releases" else sources[:1]
    photos = [(await app.bot.download(s["file_id"])).read() for s in picked]
    if mode == "plain":
        return await asyncio.to_thread(render_plain, photos[0])
    return await asyncio.to_thread(render_card, mode, make_card(post.title, style["accent"], mode), photos)


@admin.message(MusicForm.waiting_timecode, F.text)
async def on_timecode(message: Message, app: App, state: FSMContext) -> None:
    tc = parse_timecode(message.text)
    if tc is None:
        await message.answer("Не понял таймкод. Пример: <code>1:05</code> или <code>1:05-1:40</code>. Или /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    post = app.db.get_post(data["post_id"])
    if not post or post.status != "draft":
        await message.answer("Этот пост уже опубликован или удалён.")
        return

    status = await message.answer("⏳ Делаю видео с музыкой…")
    try:
        async with app.render_lock:
            await app.bot.send_chat_action(message.chat.id, "upload_video")
            image = await _first_image(app, post)
            audio = (await app.bot.download(data["audio_id"])).read()
            video = await photo_to_music_video(image, audio, *tc)
    except VideoError as e:
        await message.answer(f"⚠️ {html.escape(str(e))}")
        return
    finally:
        await status.delete()

    new_first = {
        "type": "video", "file": BufferedInputFile(video.data, "post.mp4"),
        "width": video.width, "height": video.height, "duration": video.duration,
    }
    items = [new_first] + [dict(m, file=m["file_id"]) for m in post.media[1:]]
    sent = await send_media(app.bot, message.chat.id, items, None if post.split_text else post.caption_html)
    if post.split_text:
        await message.answer(post.caption_html, link_preview_options=NO_PREVIEW)

    first = {"type": "video", "file_id": sent[0].video.file_id,
             "width": video.width, "height": video.height, "duration": video.duration}
    style = post_style(post)
    if style:
        first["style"] = style
    app.db.update_media(post.id, [first] + post.media[1:])
    mode = style["mode"] if style else None
    await message.answer(
        draft_text(post.id, mode, post.split_text, extra=" с музыкой")
        + "\n\nДругой отрывок — пришли трек ещё раз. Смена оформления уберёт музыку.",
        reply_markup=draft_kb(post.id, mode),
    )


@admin.callback_query(PostCB.filter(F.action.in_({"publish", "now"})))
async def cb_publish(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    allowed = ("draft",) if callback_data.action == "publish" else ("scheduled",)
    try:
        url = await publish_claimed(app, callback_data.id, allowed)
    except Exception as e:
        log.exception("Ошибка публикации")
        await query.answer(f"Ошибка: {e}"[:190], show_alert=True)
        return
    if url is None:
        await query.answer("Этот пост уже опубликован или отменён.", show_alert=True)
        return
    await query.answer("Опубликовано!")
    await _replace_text(query, f"✅ Пост #{callback_data.id} опубликован: {url}")


@admin.callback_query(PostCB.filter(F.action == "schedule"))
async def cb_schedule(query: CallbackQuery, callback_data: PostCB, app: App, state: FSMContext) -> None:
    post = app.db.get_post(callback_data.id)
    if not post or post.status != "draft":
        await query.answer("Этот пост уже обработан.", show_alert=True)
        return
    await state.set_state(ScheduleForm.waiting_time)
    await state.update_data(post_id=post.id)
    await query.answer()
    await query.message.answer(
        SCHEDULE_PROMPT.format(id=post.id, tz=app.cfg.tz.key),
        reply_markup=quick_kb(post.id),
    )


async def _schedule(app: App, post_id: int, when: datetime) -> str:
    post = app.db.get_post(post_id)
    if not post or post.status != "draft":
        return "Этот пост уже обработан."
    if when <= app.now():
        return "Это время уже прошло — укажи время в будущем."
    app.db.schedule(post_id, when)
    return ""


@admin.callback_query(PostCB.filter(F.action == "quick"))
async def cb_quick(query: CallbackQuery, callback_data: PostCB, app: App, state: FSMContext) -> None:
    when = quick_time(callback_data.arg, app.now())
    if error := await _schedule(app, callback_data.id, when):
        await query.answer(error, show_alert=True)
        return
    await state.clear()
    await query.answer("Запланировано")
    await query.message.edit_text(
        f"⏰ Пост #{callback_data.id} будет опубликован {app.fmt(when)}",
        reply_markup=scheduled_kb(callback_data.id),
    )


@admin.message(ScheduleForm.waiting_time, F.text)
async def on_schedule_time(message: Message, app: App, state: FSMContext) -> None:
    when = parse_when(message.text, app.now())
    if when is None:
        await message.answer("Не понял время. Пример: <code>18:30</code>, <code>завтра 10:00</code>, "
                             "<code>25.09 19:00</code>, <code>+2ч</code>. Или /cancel.")
        return
    post_id = (await state.get_data())["post_id"]
    if error := await _schedule(app, post_id, when):
        await message.answer(error)
        if "прошло" not in error:
            await state.clear()
        return
    await state.clear()
    await message.answer(
        f"⏰ Пост #{post_id} будет опубликован {app.fmt(when)}",
        reply_markup=scheduled_kb(post_id),
    )


@admin.callback_query(PostCB.filter(F.action == "unschedule"))
async def cb_unschedule(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    post = app.db.get_post(callback_data.id)
    if not post or post.status != "scheduled":
        await query.answer("Этот пост уже не в очереди.", show_alert=True)
        return
    app.db.set_status(post.id, "draft")
    await query.answer("Снят с публикации")
    style = post_style(post)
    await _replace_text(query, f"Пост #{post.id} снят с публикации — снова черновик.",
                        draft_kb(post.id, style["mode"] if style else None))


@admin.callback_query(PostCB.filter(F.action == "discard"))
async def cb_discard(query: CallbackQuery, callback_data: PostCB, app: App) -> None:
    post = app.db.get_post(callback_data.id)
    if not post or post.status != "draft":
        await query.answer("Этот пост уже обработан.", show_alert=True)
        return
    app.db.set_status(post.id, "cancelled")
    await query.answer("Удалено")
    await _replace_text(query, f"🗑 Черновик #{post.id} удалён.")


async def _replace_text(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Меняет текст сообщения с кнопками (у фото из /queue — подпись)."""
    msg = query.message
    if msg.photo or msg.video:
        await msg.edit_caption(caption=text, reply_markup=markup)
    else:
        await msg.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


# ---------- канал: подписчики ----------

channel = Router(name="channel")


def _user_link(event: ChatMemberUpdated) -> str:
    user = event.new_chat_member.user
    name = html.escape(user.full_name)
    link = f'<a href="tg://user?id={user.id}">{name}</a>'
    return f"{link} (@{user.username})" if user.username else link


@channel.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated, app: App) -> None:
    if event.chat.id != app.channel_id:
        return
    app.db.add_member_event(event.new_chat_member.user.id, "join")
    count = await app.bot.get_chat_member_count(event.chat.id)
    threshold = app.cfg.subscribers_threshold

    if count <= threshold:
        await app.notify_admins(f"🆕 Новый подписчик: {_user_link(event)}\n👥 Всего: {count}/{threshold}")
    if count >= threshold and not app.db.get_setting("threshold_reached"):
        app.db.set_setting("threshold_reached", "1")
        await app.notify_admins(
            f"🎉 В канале {count} подписчиков! Больше не пишу про каждого — "
            f"теперь каждый день в {app.cfg.stats_time.strftime('%H:%M')} присылаю статистику."
        )


@channel.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_leave(event: ChatMemberUpdated, app: App) -> None:
    if event.chat.id == app.channel_id:
        app.db.add_member_event(event.new_chat_member.user.id, "leave")


# ---------- запуск ----------

async def check_channel(app: App) -> None:
    """Проверяет, что бот — админ канала с правом публикации."""
    try:
        chat = await app.bot.get_chat(app.cfg.channel)
        app.channel_id, app.channel_username = chat.id, chat.username
        me = await app.bot.get_me()
        member = await app.bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        msg = f"Не могу получить доступ к каналу {app.cfg.channel}: {e}. Добавь бота в админы канала."
        log.error(msg)
        await app.notify_admins(f"⚠️ {html.escape(msg)}")
        return

    # шапка карточек: имя и аватарка канала (если не выйдет — останутся из макета)
    try:
        avatar = (await app.bot.download(chat.photo.big_file_id)).read() if chat.photo else None
        set_brand(chat.username, avatar)
    except Exception:
        log.exception("Не удалось взять аватарку канала")

    if member.status != ChatMemberStatus.ADMINISTRATOR or not getattr(member, "can_post_messages", False):
        msg = "Бот не админ канала или у него нет права публиковать сообщения."
        log.error(msg)
        await app.notify_admins(f"⚠️ {msg}")
    else:
        log.info("Канал: %s (%s)", chat.title, chat.id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = App(bot=bot, cfg=cfg, db=Database(cfg.db_path))

    admin.message.filter(F.chat.type == "private", F.from_user.id.in_(cfg.admin_ids))
    admin.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))

    dp = Dispatcher()
    dp["app"] = app
    dp.include_routers(admin, channel)

    await check_channel(app)
    await bot.set_my_commands([
        BotCommand(command="queue", description="Отложенные посты"),
        BotCommand(command="stats", description="Статистика за сегодня"),
        BotCommand(command="help", description="Как сделать пост"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ])

    tasks = [asyncio.create_task(scheduler_loop(app)), asyncio.create_task(daily_stats_loop(app))]
    try:
        # chat_member приходит только если явно его запросить
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
