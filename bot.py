"""Telegram-бот: поиск песен, эффекты, теги, посты в канал.

Автоопределение:
  • аудиофайл                    -> эффекты (speed up / slowed, reverb, 8D) + изменение тегов
  • голосовое / видео / кружок   -> поиск песни и отправка аудиофайла
  • ссылка                       -> поиск песни и отправка аудиофайла
  • текст                        -> поиск по названию (выбор из списка кнопок)
  • /start                       -> меню функций, /post -> пост в канал
"""
import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from shazamio import Shazam
from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func, match_filter_func

load_dotenv()

# =====================  НАСТРОЙКИ  =====================

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")

# Канал для постов (@username или числовой id вида -100...) и админы бота (id через запятую)
_ch = os.getenv("CHANNEL_ID", "").strip()
try:
    CHANNEL_ID: int | str = int(_ch)
except ValueError:
    CHANNEL_ID = _ch
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# Подпись под каждым отправленным треком
SPEEDUP_URL = "https://t.me/speedupaudioe"
TIPOKE_URL = "https://t.me/tipokeee"
CAPTION = (f'<a href="{SPEEDUP_URL}">Speed Up Audio</a> | '
           f'<a href="{TIPOKE_URL}">Tipoke Music</a>')

# Куки для yt-dlp (нужны, если YouTube блокирует серверный IP). Путь к файлу cookies.txt
COOKIES_TMP = None
_cookies_src = os.getenv("COOKIES_FILE")
if _cookies_src and Path(_cookies_src).exists():
    COOKIES_TMP = os.path.join(tempfile.gettempdir(), "cookies.txt")
    shutil.copyfile(_cookies_src, COOKIES_TMP)

MAX_TG_DOWNLOAD = 20 * 1024 * 1024   # лимит Bot API на скачивание файлов
MAX_TG_UPLOAD = 50 * 1024 * 1024     # лимит Bot API на отправку файлов
MAX_TRACK_DURATION = 10 * 60         # не качаем ролики длиннее 10 минут
SAMPLE_SECONDS = 25                  # фрагмент для распознавания
MIN_SPEED, MAX_SPEED = 0.25, 3.0
URL_RE = re.compile(r"https?://\S+")
BTN_RE = re.compile(r"^(?P<text>.+?)\s+[-–—]\s+(?P<url>(?:https?://|t\.me/)\S+)$")

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
shazam = Shazam()


class Fx(StatesGroup):
    custom_speed = State()
    tags = State()


class Post(StatesGroup):
    content = State()
    buttons = State()
    confirm = State()


# =====================  ОБЩИЕ УТИЛИТЫ  =====================

def ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True}
    if COOKIES_TMP:
        opts["cookiefile"] = COOKIES_TMP
    opts.update(extra)
    return opts


async def run_ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg не смог обработать файл")


def meta_args(title: str, artist: str) -> list[str]:
    args = ["-id3v2_version", "3", "-metadata", f"title={title}"]
    if artist:
        args += ["-metadata", f"artist={artist}"]
    return args


async def retag(src: Path, dst: Path, title: str, artist: str, copy: bool = True) -> None:
    """Меняет теги. copy=True — без перекодирования (только для mp3)."""
    codec = ["-c:a", "copy"] if copy else ["-c:a", "libmp3lame", "-b:a", "256k"]
    await run_ffmpeg("-i", str(src), "-map", "0:a:0", *codec, *meta_args(title, artist), str(dst))


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", name).strip()[:100] or "track"


def clean_title(title: str) -> str:
    return re.sub(r"\s*[\(\[](official|lyrics?|audio|video|music video|hd|4k)[^\)\]]*[\)\]]",
                  "", title, flags=re.I).strip()


def parse_artist_title(text: str, default_artist: str = "") -> tuple[str, str]:
    """'Исполнитель - Название' -> (artist, title)."""
    if " - " in text:
        artist, title = (x.strip() for x in text.split(" - ", 1))
        return artist, title
    return default_artist, text.strip()


def guess_tags(info: dict) -> tuple[str, str]:
    """Название и исполнитель из метаданных YouTube."""
    title = clean_title(info.get("track") or info.get("title") or "Track")
    artist = info.get("artist") or ""
    if not artist:
        if " - " in title:
            artist, title = parse_artist_title(title)
        else:
            artist = (info.get("uploader") or info.get("channel") or "").removesuffix(" - Topic")
    return title, artist


async def send_mp3(target: Message, mp3: Path, title: str, artist: str) -> bool:
    """Отправляет mp3 с подписью-ссылками. Возвращает False, если файл слишком большой."""
    if mp3.stat().st_size > MAX_TG_UPLOAD:
        await target.answer("⚠️ Файл больше 50 МБ, Telegram не даёт его отправить.")
        return False
    name = safe_name(f"{artist} - {title}" if artist else title)
    await target.answer_audio(
        FSInputFile(mp3, filename=f"{name}.mp3"),
        title=title, performer=artist or None, caption=CAPTION,
    )
    return True


# =====================  ЭФФЕКТЫ И ТЕГИ  =====================

@dataclass
class Session:
    user_id: int
    file_id: str
    ext: str
    title: str
    artist: str
    speed: float = 1.0
    reverb: bool = False
    eightd: bool = False
    tags_changed: bool = False

    @property
    def has_effects(self) -> bool:
        return abs(self.speed - 1) > 1e-9 or self.reverb or self.eightd


SESSIONS: dict[str, Session] = {}
PRESETS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]


def new_session(**kwargs) -> str:
    sid = uuid.uuid4().hex[:8]
    SESSIONS[sid] = Session(**kwargs)
    while len(SESSIONS) > 500:           # защита от разрастания памяти
        SESSIONS.pop(next(iter(SESSIONS)))
    return sid


def effects_kb(sid: str, s: Session) -> InlineKeyboardMarkup:
    def spd(v: float) -> InlineKeyboardButton:
        mark = "✅ " if abs(s.speed - v) < 1e-9 else ""
        return InlineKeyboardButton(text=f"{mark}{v:g}x", callback_data=f"fx:{sid}:spd:{v}")

    custom_active = all(abs(s.speed - v) > 1e-9 for v in PRESETS)
    custom_text = f"✅ ✏️ {s.speed:g}x" if custom_active else "✏️ своё"

    b = InlineKeyboardBuilder()
    b.row(*[spd(v) for v in (0.5, 0.6, 0.7, 0.8, 0.9)])
    b.row(*[spd(v) for v in (1.0, 1.1, 1.25, 1.5)])
    b.row(spd(1.75), spd(2.0),
          InlineKeyboardButton(text=custom_text, callback_data=f"fx:{sid}:custom"))
    b.row(
        InlineKeyboardButton(text=f"🌊 Reverb {'✅' if s.reverb else '⬜'}", callback_data=f"fx:{sid}:rev"),
        InlineKeyboardButton(text=f"🎧 8D {'✅' if s.eightd else '⬜'}", callback_data=f"fx:{sid}:8d"),
    )
    b.row(InlineKeyboardButton(text="🏷 Изменить теги", callback_data=f"fx:{sid}:tags"))
    b.row(InlineKeyboardButton(text="▶️ Создать", callback_data=f"fx:{sid}:go"))
    return b.as_markup()


def menu_text(s: Session) -> str:
    if s.speed < 1:
        speed = f"{s.speed:g}x (slowed)"
    elif s.speed > 1:
        speed = f"{s.speed:g}x (speed up)"
    else:
        speed = "1x (без изменений)"
    return (
        "🎛 <b>Выбери эффекты</b>\n\n"
        f"🏷 Теги: <b>{html.escape(s.artist or '—')}</b> — <b>{html.escape(s.title)}</b>\n"
        f"⏱ Скорость: <b>{speed}</b>\n"
        f"🌊 Reverb: <b>{'вкл' if s.reverb else 'выкл'}</b>\n"
        f"🎧 8D: <b>{'вкл' if s.eightd else 'выкл'}</b> (слушай в наушниках)\n\n"
        "Скорость меняет и тон, как в обычных slowed / sped up ремиксах. "
        "Эффекты можно комбинировать. Можно изменить только теги, без эффектов. "
        "Потом жми «Создать»."
    )


def build_filter(s: Session) -> str:
    f = ["aresample=44100"]
    if abs(s.speed - 1) > 1e-9:
        f += [f"asetrate={round(44100 * s.speed)}", "aresample=44100"]
    if s.eightd:
        f.append("apulsator=mode=sine:hz=0.125:offset_l=0:offset_r=0.5:amount=0.9")
    if s.reverb:
        f.append("aecho=0.8:0.88:60|120|200|320|480:0.35|0.3|0.25|0.2|0.15")
    f.append("alimiter=limit=0.95")
    return ",".join(f)


async def apply_effects(src: Path, dst: Path, s: Session) -> None:
    await run_ffmpeg("-i", str(src), "-vn", "-ac", "2", "-af", build_filter(s),
                     "-c:a", "libmp3lame", "-b:a", "192k", *meta_args(s.title, s.artist), str(dst))


# =====================  ПОИСК ПЕСНИ  =====================

async def make_sample(src: Path, dst: Path) -> None:
    await run_ffmpeg("-i", str(src), "-t", str(SAMPLE_SECONDS),
                     "-vn", "-ac", "1", "-ar", "44100", str(dst))


def _download_from_url(url: str, out_dir: str) -> Path:
    """Первая минута по ссылке — только для распознавания."""
    opts = ydl_opts(
        format="bestaudio/best", outtmpl=os.path.join(out_dir, "dl.%(ext)s"), noplaylist=True,
        download_ranges=download_range_func(None, [(0, 60)]), max_filesize=100 * 1024 * 1024,
    )
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(out_dir).glob("dl.*") if p.suffix != ".part"]
    if not files:
        raise RuntimeError("Не удалось скачать по ссылке")
    return files[0]


def _search_youtube(query: str) -> list[dict]:
    with YoutubeDL(ydl_opts(extract_flat=True)) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
    return [e for e in (info.get("entries", []) if info else []) if e.get("id")]


def _find_video_id(query: str) -> str | None:
    for e in _search_youtube(query):
        d = e.get("duration")
        if d is None or d <= MAX_TRACK_DURATION:
            return e["id"]
    return None


def _download_mp3(video_id: str, out_dir: str) -> tuple[Path, dict]:
    """Скачивает аудио целиком и конвертирует в mp3."""
    opts = ydl_opts(
        format="bestaudio/best", outtmpl=os.path.join(out_dir, "track.%(ext)s"), noplaylist=True,
        max_filesize=100 * 1024 * 1024,
        match_filter=match_filter_func(f"duration <= {MAX_TRACK_DURATION}"),
        postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    )
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    mp3 = Path(out_dir) / "track.mp3"
    if not mp3.exists():
        raise RuntimeError("Не удалось скачать (трек слишком длинный или недоступен)")
    return mp3, info or {}


def links_block(artist: str, title: str) -> str:
    q = quote_plus(f"{artist} {title}".strip())
    return (
        f'🔗 <a href="https://open.spotify.com/search/{q}">Spotify</a> · '
        f'<a href="https://music.youtube.com/search?q={q}">YouTube Music</a> · '
        f'<a href="https://music.apple.com/search?term={q}">Apple Music</a>'
    )


def format_track(track: dict) -> str:
    title = track.get("title", "—")
    artist = track.get("subtitle", "—")
    text = f"🎵 <b>{html.escape(title)}</b>\n👤 {html.escape(artist)}\n\n"
    if track.get("url"):
        text += f'🔎 <a href="{track["url"]}">Страница в Shazam</a>\n'
    return text + links_block(artist, title)


async def recognize_track(path: Path) -> dict | None:
    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "sample.wav"
        await make_sample(path, sample)
        result = await shazam.recognize(str(sample))
    return result.get("track")


async def deliver_recognized(message: Message, status: Message, track: dict) -> None:
    """Показывает инфо о найденном треке и отправляет аудиофайл."""
    info_text = format_track(track)
    title, artist = track.get("title", "Track"), track.get("subtitle", "")
    await status.edit_text(info_text + "\n\n⬇️ Скачиваю аудио...", disable_web_page_preview=True)
    try:
        video_id = await asyncio.to_thread(_find_video_id, f"{artist} - {title}")
        if not video_id:
            raise RuntimeError("не нашёл аудио")
        with tempfile.TemporaryDirectory() as tmp:
            mp3, _ = await asyncio.to_thread(_download_mp3, video_id, tmp)
            final = Path(tmp) / "final.mp3"
            await retag(mp3, final, title, artist)
            await send_mp3(message, final, title, artist)
    except Exception:
        logging.exception("deliver error")
        await message.answer("⚠️ Песню нашёл, но скачать аудио не получилось. Попробуй позже.")
    await status.edit_text(info_text, disable_web_page_preview=True)


# =====================  /start И МЕНЮ  =====================

def start_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔎 Найти песню", callback_data="menu:search"))
    b.row(InlineKeyboardButton(text="🎛 Эффекты", callback_data="menu:fx"))
    b.row(InlineKeyboardButton(text="🏷 Изменить теги", callback_data="menu:tags"))
    b.row(InlineKeyboardButton(text="📢 Пост в канал", callback_data="menu:post"))
    return b.as_markup()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Выбери функцию кнопкой ниже или просто пришли мне:\n\n"
        "• <b>аудиофайл</b> — сделаю эффекты или поменяю теги\n"
        "• <b>голосовое, видео, кружок или ссылку</b> — найду песню и пришлю аудио\n"
        "• <b>название</b> — поищу трек",
        reply_markup=start_kb(),
    )


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Пришли аудио, видео, ссылку или название 🎧")


@dp.callback_query(F.data.startswith("menu:"))
async def on_menu(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":", 1)[1]
    await cb.answer()
    if action == "search":
        await cb.message.answer("🔎 Пришли голосовое, видео, кружок или ссылку на видео, "
                                "либо напиши название песни.")
    elif action == "fx":
        await cb.message.answer("🎛 Пришли аудиофайл (mp3, m4a...), и я открою меню эффектов: "
                                "speed up / slowed, reverb, 8D.")
    elif action == "tags":
        await cb.message.answer("🏷 Пришли аудиофайл. В меню под ним будет кнопка «Изменить теги»: "
                                "можно поменять только их, без эффектов.")
    elif action == "post":
        await begin_post(cb.message, cb.from_user.id, state)


# =====================  ПОСТЫ В КАНАЛ  =====================

def post_ready(user_id: int) -> str | None:
    """Возвращает текст ошибки или None, если всё готово."""
    if not CHANNEL_ID or not ADMIN_IDS:
        return ("Посты не настроены. Укажи переменные окружения CHANNEL_ID (канал) "
                "и ADMIN_IDS (твой Telegram id, узнать можно у @userinfobot).")
    if user_id not in ADMIN_IDS:
        return "Эта функция доступна только админам бота."
    return None


def rows_to_kb(rows: list | None) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, url=u) for t, u in row] for row in rows
    ])


def parse_buttons(text: str) -> list | None:
    """Каждая строка — ряд кнопок. Кнопки в ряду разделяются знаком |."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for part in line.split("|"):
            m = BTN_RE.match(part.strip())
            if not m:
                return None
            url = m["url"]
            if url.startswith("t.me/"):
                url = "https://" + url
            row.append((m["text"].strip(), url))
        rows.append(row)
    return rows or None


async def begin_post(message: Message, user_id: int, state: FSMContext) -> None:
    error = post_ready(user_id)
    if error:
        await message.answer(error)
        return
    await state.clear()
    await state.set_state(Post.content)
    await message.answer(
        f"📢 Пост в <b>{html.escape(str(CHANNEL_ID))}</b>\n\n"
        "Пришли сам пост: текст, фото, видео, аудио или файл (с подписью, если нужна). "
        "Форматирование сохранится.\n\nОтмена: /cancel"
    )


@dp.message(Command("post"))
async def cmd_post(message: Message, state: FSMContext):
    await begin_post(message, message.from_user.id, state)


@dp.message(Post.content)
async def post_content(message: Message, state: FSMContext):
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id)
    await state.set_state(Post.buttons)
    skip = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Без кнопок", callback_data="post:skip"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="post:cancel"),
    ]])
    await message.answer(
        "Теперь кнопки под постом. Каждая строка — один ряд, формат:\n\n"
        "<code>Текст кнопки - https://ссылка</code>\n\n"
        "Несколько кнопок в одном ряду разделяй знаком |:\n"
        "<code>Канал - https://t.me/speedupaudioe | Музыка - https://t.me/tipokeee</code>\n\n"
        "Или нажми «Без кнопок».",
        reply_markup=skip, disable_web_page_preview=True,
    )


async def show_preview(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    await bot.copy_message(chat_id, data["src_chat"], data["src_msg"],
                           reply_markup=rows_to_kb(data.get("rows")))
    await state.set_state(Post.confirm)
    confirm = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data="post:publish"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="post:cancel"),
    ]])
    await bot.send_message(chat_id, f"👆 Так пост будет выглядеть в канале "
                                    f"<b>{html.escape(str(CHANNEL_ID))}</b>. Публикуем?",
                           reply_markup=confirm)


@dp.message(Post.buttons, F.text)
async def post_buttons(message: Message, state: FSMContext, bot: Bot):
    rows = parse_buttons(message.text)
    if not rows:
        await message.reply("Не разобрал кнопки 🤔 Формат строки: "
                            "<code>Текст - https://ссылка</code>. Попробуй ещё раз или нажми «Без кнопок».")
        return
    await state.update_data(rows=rows)
    await show_preview(bot, message.chat.id, state)


@dp.callback_query(F.data == "post:cancel")
async def post_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await cb.message.answer("Отменено.")


@dp.callback_query(Post.buttons, F.data == "post:skip")
async def post_skip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    await state.update_data(rows=None)
    await show_preview(bot, cb.message.chat.id, state)


@dp.callback_query(Post.confirm, F.data == "post:publish")
async def post_publish(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if post_ready(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    await cb.answer()
    try:
        await bot.copy_message(CHANNEL_ID, data["src_chat"], data["src_msg"],
                               reply_markup=rows_to_kb(data.get("rows")))
        await cb.message.answer("✅ Пост опубликован!")
        await state.clear()
    except TelegramAPIError as e:
        await cb.message.answer(
            f"⚠️ Не получилось опубликовать: {html.escape(e.message)}\n\n"
            "Проверь, что бот добавлен в канал администратором с правом публикации "
            "и что CHANNEL_ID указан верно.")


# =====================  ВВОД ЗНАЧЕНИЙ (скорость / теги)  =====================

@dp.message(Fx.custom_speed, F.text)
async def on_custom_speed(message: Message, state: FSMContext):
    data = await state.get_data()
    s = SESSIONS.get(data.get("sid"))
    try:
        value = float(message.text.strip().replace(",", "."))
    except ValueError:
        value = None
    if value is None or not MIN_SPEED <= value <= MAX_SPEED:
        await message.reply(f"Нужно число от {MIN_SPEED:g} до {MAX_SPEED:g}, например 0.85. "
                            "Или /cancel.")
        return
    await state.clear()
    if not s:
        await message.reply("Сессия устарела, пришли файл заново.")
        return
    s.speed = value
    await message.answer(menu_text(s), reply_markup=effects_kb(data["sid"], s))


@dp.message(Fx.tags, F.text)
async def on_tags_input(message: Message, state: FSMContext):
    data = await state.get_data()
    s = SESSIONS.get(data.get("sid"))
    await state.clear()
    if not s:
        await message.reply("Сессия устарела, пришли файл заново.")
        return
    s.artist, s.title = parse_artist_title(message.text, default_artist=s.artist)
    s.tags_changed = True
    await message.answer(menu_text(s), reply_markup=effects_kb(data["sid"], s))


# =====================  АВТООПРЕДЕЛЕНИЕ ТИПА СООБЩЕНИЯ  =====================

# аудиофайл -> эффекты и теги
@dp.message(F.audio | F.document.mime_type.startswith("audio/"))
async def on_audio_file(message: Message, state: FSMContext):
    await state.clear()
    media = message.audio or message.document
    if media.file_size and media.file_size > MAX_TG_DOWNLOAD:
        await message.reply("Файл больше 20 МБ, Telegram не даёт мне его скачать.")
        return

    file_name = media.file_name or ""
    ext = Path(file_name).suffix.lower() or (".mp3" if media.mime_type == "audio/mpeg" else "")
    title = getattr(media, "title", None)
    artist = getattr(media, "performer", None)
    if not title:
        artist_guess, title = parse_artist_title(Path(file_name).stem or "Track")
        artist = artist or artist_guess
    sid = new_session(user_id=message.from_user.id, file_id=media.file_id, ext=ext,
                      title=title, artist=artist or "")
    s = SESSIONS[sid]
    await message.reply(menu_text(s), reply_markup=effects_kb(sid, s))


# гс / видео / кружок -> поиск песни
@dp.message(F.voice | F.video | F.video_note)
async def on_media_recognize(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    media = message.voice or message.video or message.video_note
    if media.file_size and media.file_size > MAX_TG_DOWNLOAD:
        await message.reply("Файл больше 20 МБ, Telegram не даёт мне его скачать. Пришли покороче.")
        return
    status = await message.reply("🎧 Слушаю...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            await bot.download(media.file_id, destination=src)
            track = await recognize_track(src)
    except Exception:
        logging.exception("media error")
        await status.edit_text("⚠️ Не получилось обработать файл.")
        return
    if not track:
        await status.edit_text("😕 Не смог узнать трек. Попробуй фрагмент, где музыка слышна чётче.")
        return
    await deliver_recognized(message, status, track)


# текст: ссылка -> поиск песни, иначе поиск по названию
@dp.message(F.text)
async def on_text(message: Message):
    text = message.text.strip()
    url_match = URL_RE.search(text)
    status = await message.reply("🔎 Ищу...")
    try:
        if url_match:
            with tempfile.TemporaryDirectory() as tmp:
                path = await asyncio.to_thread(_download_from_url, url_match.group(0), tmp)
                track = await recognize_track(path)
            if not track:
                await status.edit_text("😕 Не смог узнать трек по этой ссылке.")
                return
            await deliver_recognized(message, status, track)
            return

        entries = await asyncio.to_thread(_search_youtube, text)
        if not entries:
            await status.edit_text("😕 Ничего не нашёл.")
            return
        b = InlineKeyboardBuilder()
        for e in entries:
            d = e.get("duration")
            dur = f" ({int(d) // 60}:{int(d) % 60:02d})" if d else ""
            b.row(InlineKeyboardButton(text=f"🎵 {e.get('title', 'без названия')[:48]}{dur}",
                                       callback_data=f"dl:{e['id']}"))
        await status.edit_text(f"🔎 Результаты по запросу <b>{html.escape(text)}</b>.\n"
                               "Выбери трек, и я пришлю аудио:", reply_markup=b.as_markup())
    except Exception:
        logging.exception("text error")
        await status.edit_text("⚠️ Что-то пошло не так. Проверь ссылку или попробуй позже.")


# =====================  КНОПКИ: скачать трек / эффекты  =====================

@dp.callback_query(F.data.startswith("dl:"))
async def on_download(cb: CallbackQuery):
    video_id = cb.data.split(":", 1)[1]
    await cb.answer("Скачиваю...")
    status = await cb.message.answer("⬇️ Скачиваю аудио...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp3, info = await asyncio.to_thread(_download_mp3, video_id, tmp)
            title, artist = guess_tags(info)
            final = Path(tmp) / "final.mp3"
            await retag(mp3, final, title, artist)
            await send_mp3(cb.message, final, title, artist)
        await status.delete()
    except Exception:
        logging.exception("download error")
        await status.edit_text("⚠️ Не получилось скачать. Выбери другой трек или попробуй позже.")


async def refresh_menu(cb: CallbackQuery, sid: str, s: Session) -> None:
    try:
        await cb.message.edit_text(menu_text(s), reply_markup=effects_kb(sid, s))
    except TelegramBadRequest:
        pass  # "message is not modified"


@dp.callback_query(F.data.startswith("fx:"))
async def on_fx(cb: CallbackQuery, bot: Bot, state: FSMContext):
    _, sid, action, *rest = cb.data.split(":")
    s = SESSIONS.get(sid)
    if not s:
        await cb.answer("Сессия устарела, пришли файл заново", show_alert=True)
        return
    if cb.from_user.id != s.user_id:
        await cb.answer("Это не твой трек 🙂", show_alert=True)
        return

    if action == "spd":
        s.speed = float(rest[0])
        await refresh_menu(cb, sid, s)
    elif action == "rev":
        s.reverb = not s.reverb
        await refresh_menu(cb, sid, s)
    elif action == "8d":
        s.eightd = not s.eightd
        await refresh_menu(cb, sid, s)
    elif action == "custom":
        await state.set_state(Fx.custom_speed)
        await state.update_data(sid=sid)
        await cb.message.answer(f"Напиши скорость числом от {MIN_SPEED:g} до {MAX_SPEED:g}, "
                                "например 0.85 или 1.3")
    elif action == "tags":
        await state.set_state(Fx.tags)
        await state.update_data(sid=sid)
        await cb.message.answer(
            "Напиши новые теги в формате:\n<code>Исполнитель - Название</code>\n\n"
            f"Сейчас: {html.escape(s.artist or '—')} - {html.escape(s.title)}\n"
            "Если напишешь только название, исполнитель останется прежним.")
    elif action == "go":
        if not s.has_effects and not s.tags_changed:
            await cb.answer("Выбери эффект или измени теги", show_alert=True)
            return
        await cb.answer()
        await render(cb, bot, sid, s)
        return
    await cb.answer()


async def render(cb: CallbackQuery, bot: Bot, sid: str, s: Session) -> None:
    await cb.message.edit_text("⏳ Обрабатываю, это может занять до минуты...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            await bot.download(s.file_id, destination=src)
            out = Path(tmp) / "result.mp3"
            if s.has_effects:
                await apply_effects(src, out, s)
            else:                                   # только теги
                await retag(src, out, s.title, s.artist, copy=(s.ext == ".mp3"))
            ok = await send_mp3(cb.message, out, s.title, s.artist)
        if ok:
            await cb.message.edit_text("✅ Готово! Можешь выбрать другие эффекты:\n\n" + menu_text(s),
                                       reply_markup=effects_kb(sid, s))
        else:
            await cb.message.edit_text(menu_text(s), reply_markup=effects_kb(sid, s))
    except Exception:
        logging.exception("render error")
        await cb.message.edit_text("⚠️ Не получилось обработать файл. Попробуй другой.",
                                   reply_markup=effects_kb(sid, s))


# =====================  ЗАПУСК  =====================

async def start_health_server() -> None:
    """Если Render задал PORT (Web Service) — поднимаем мини-сервер для health check."""
    port = os.getenv("PORT")
    if not port:
        return
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()


async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню функций"),
        BotCommand(command="post", description="Пост в канал (для админов)"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ])
    await start_health_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
