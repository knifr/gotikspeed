"""Telegram-бот: поиск песен, эффекты, теги и обложка, посты в канал.

Автоопределение:
  • аудиофайл                    -> эффекты (speed up / slowed, reverb, 8D), теги, обложка
  • голосовое / видео / кружок   -> поиск песни и отправка аудиофайла
  • ссылка                       -> поиск песни и отправка аудиофайла
  • текст                        -> сразу присылает найденный трек + другие варианты кнопками
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

import aiohttp
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

# Где искать аудио, по порядку. Если источник не работает, бот пробует следующий.
PREFIX = {"youtube": "ytsearch", "soundcloud": "scsearch"}
SOURCES = [s.strip() for s in os.getenv("SEARCH_SOURCES", "youtube,soundcloud").split(",")
           if s.strip() in PREFIX] or ["youtube", "soundcloud"]
ICON = {"youtube": "▶️", "soundcloud": "☁️"}

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
    cover = State()


class Post(StatesGroup):
    content = State()
    buttons = State()
    confirm = State()


class TrackSkipped(Exception):
    """Трек не подошёл (слишком длинный и т.п.), сам источник при этом работает."""


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


def cover_map_args(cover: Path | None, keep: bool = True) -> list[str]:
    """Обложка: новая (вторым входом), старая из файла или никакой."""
    if cover:
        return ["-map", "1:0", "-c:v", "copy",
                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    if keep:
        return ["-map", "0:v:0?", "-c:v", "copy"]
    return []


async def retag(src: Path, dst: Path, title: str, artist: str, copy: bool = True,
                cover: Path | None = None, keep_cover: bool = True) -> None:
    """Меняет теги и обложку. copy=True — звук без перекодирования (только для mp3)."""
    inputs = ["-i", str(src)] + (["-i", str(cover)] if cover else [])
    codec = ["-c:a", "copy"] if copy else ["-c:a", "libmp3lame", "-b:a", "256k"]
    await run_ffmpeg(*inputs, "-map", "0:a:0", *cover_map_args(cover, keep_cover),
                     *codec, *meta_args(title, artist), str(dst))


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
    """Название и исполнитель из метаданных источника."""
    title = clean_title(info.get("track") or info.get("title") or "Track")
    artist = info.get("artist") or ""
    if not artist:
        if " - " in title:
            artist, title = parse_artist_title(title)
        else:
            artist = (info.get("uploader") or info.get("channel") or "").removesuffix(" - Topic")
    return title, artist


def explain_error(e: Exception) -> str:
    s = str(e)
    if "Sign in to confirm" in s or "not a bot" in s:
        return "YouTube блокирует сервер (нужны cookies, см. README)"
    first = s.strip().splitlines()[0] if s.strip() else type(e).__name__
    return first[:160]


# ---------- обложки ----------

async def fetch_image(url: str, dst: Path) -> bool:
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    return False
                dst.write_bytes(await r.read())
                return True
    except Exception:
        logging.warning("cover download failed", exc_info=True)
        return False


async def make_square_jpg(raw: Path, dst: Path, size: int) -> None:
    vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}"
    await run_ffmpeg("-i", str(raw), "-vf", vf, "-frames:v", "1", "-q:v", "3", str(dst))


async def prepare_cover(raw: Path, tmp: Path) -> tuple[Path | None, Path | None]:
    """Из любой картинки делает квадратную обложку (600 px) и миниатюру для Telegram (320 px)."""
    cover, thumb = tmp / "cover.jpg", tmp / "thumb.jpg"
    try:
        await make_square_jpg(raw, cover, 600)
        await make_square_jpg(raw, thumb, 320)
        return cover, thumb
    except Exception:
        logging.warning("cover prepare failed", exc_info=True)
        return None, None


# ---------- отправка ----------

async def send_mp3(target: Message, mp3: Path, title: str, artist: str,
                   thumb: Path | None = None) -> bool:
    """Отправляет mp3 с подписью-ссылками. Возвращает False, если файл слишком большой."""
    if mp3.stat().st_size > MAX_TG_UPLOAD:
        await target.answer("⚠️ Файл больше 50 МБ, Telegram не даёт его отправить.")
        return False
    name = safe_name(f"{artist} - {title}" if artist else title)
    await target.answer_audio(
        FSInputFile(mp3, filename=f"{name}.mp3"),
        title=title, performer=artist or None, caption=CAPTION,
        thumbnail=FSInputFile(thumb) if thumb else None,
    )
    return True


async def finalize_and_send(target: Message, tmp: str, mp3: Path, title: str, artist: str,
                            cover_url: str | None = None) -> bool:
    """Вшивает теги и обложку в скачанный mp3 и отправляет."""
    tmp_dir = Path(tmp)
    cover = thumb = None
    if cover_url:
        raw = tmp_dir / "cover_raw"
        if await fetch_image(cover_url, raw):
            cover, thumb = await prepare_cover(raw, tmp_dir)
    final = tmp_dir / "final.mp3"
    try:
        await retag(mp3, final, title, artist, cover=cover)
    except Exception:
        logging.warning("retag with cover failed, retry without", exc_info=True)
        thumb = None
        await retag(mp3, final, title, artist, keep_cover=False)
    return await send_mp3(target, final, title, artist, thumb)


# =====================  ЭФФЕКТЫ, ТЕГИ, ОБЛОЖКА  =====================

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
    tags_changed: bool = False          # менялись теги или обложка
    cover_file_id: str | None = None

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
    b.row(
        InlineKeyboardButton(text="🏷 Теги", callback_data=f"fx:{sid}:tags"),
        InlineKeyboardButton(text=f"🖼 Обложка {'✅' if s.cover_file_id else ''}".strip(),
                             callback_data=f"fx:{sid}:cover"),
    )
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
        f"🖼 Обложка: <b>{'новая' if s.cover_file_id else 'как в файле'}</b>\n"
        f"⏱ Скорость: <b>{speed}</b>\n"
        f"🌊 Reverb: <b>{'вкл' if s.reverb else 'выкл'}</b>\n"
        f"🎧 8D: <b>{'вкл' if s.eightd else 'выкл'}</b> (слушай в наушниках)\n\n"
        "Скорость меняет и тон, как в обычных slowed / sped up ремиксах. "
        "Эффекты можно комбинировать. Теги и обложку можно менять и без эффектов. "
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


async def apply_effects(src: Path, dst: Path, s: Session, cover: Path | None = None,
                        keep_cover: bool = True) -> None:
    inputs = ["-i", str(src)] + (["-i", str(cover)] if cover else [])
    await run_ffmpeg(*inputs, "-map", "0:a:0", *cover_map_args(cover, keep_cover),
                     "-ac", "2", "-af", build_filter(s),
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


def _search(query: str, per_source: int = 3) -> list[dict]:
    """Ищет аудио во всех источниках: сначала YouTube, потом SoundCloud."""
    results: list[dict] = []
    for src in SOURCES:
        try:
            with YoutubeDL(ydl_opts(extract_flat=True)) as ydl:
                info = ydl.extract_info(f"{PREFIX[src]}{per_source}:{query}", download=False)
            for e in (info.get("entries") or []) if info else []:
                if e and (e.get("url") or e.get("id")):
                    e["_src"] = src
                    results.append(e)
        except Exception:
            logging.exception("search failed: %s", src)
    return results


def entry_url(e: dict) -> str:
    u = e.get("url") or ""
    return u if u.startswith("http") else f"https://www.youtube.com/watch?v={e['id']}"


def _download_mp3(url: str, out_dir: str) -> tuple[Path, dict]:
    """Скачивает аудио целиком и конвертирует в mp3."""
    opts = ydl_opts(
        format="bestaudio/best", outtmpl=os.path.join(out_dir, "track.%(ext)s"), noplaylist=True,
        max_filesize=100 * 1024 * 1024,
        match_filter=match_filter_func(f"duration <= {MAX_TRACK_DURATION}"),
        postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    )
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    mp3 = Path(out_dir) / "track.mp3"
    if not mp3.exists():
        raise TrackSkipped("трек слишком длинный или недоступен")
    return mp3, info or {}


def _download_best(entries: list[dict], out_dir: str) -> tuple[Path, dict, dict]:
    """Пробует результаты по очереди. Если источник сломан, переходит к следующему."""
    failed: set[str] = set()
    last: Exception | None = None
    for e in entries:
        if e["_src"] in failed:
            continue
        d = e.get("duration")
        if d and d > MAX_TRACK_DURATION:
            continue
        for p in Path(out_dir).glob("track.*"):
            p.unlink(missing_ok=True)
        try:
            mp3, info = _download_mp3(entry_url(e), out_dir)
            return mp3, info, e
        except TrackSkipped as ex:
            last = ex
        except Exception as ex:
            logging.warning("download failed (%s): %s", e["_src"], ex)
            last = ex
            failed.add(e["_src"])
    raise last or RuntimeError("не нашёл подходящий трек")


RESULTS: dict[str, str] = {}   # короткий ключ кнопки -> ссылка на трек


def remember(e: dict) -> str:
    key = uuid.uuid4().hex[:8]
    RESULTS[key] = entry_url(e)
    while len(RESULTS) > 1000:
        RESULTS.pop(next(iter(RESULTS)))
    return key


async def send_alternatives(message: Message, entries: list[dict], used: dict | None) -> None:
    alts = [e for e in entries if e is not used][:5]
    if not alts:
        return
    b = InlineKeyboardBuilder()
    for e in alts:
        d = e.get("duration")
        dur = f" ({int(d) // 60}:{int(d) % 60:02d})" if d else ""
        title = (e.get("title") or "без названия")[:46]
        b.row(InlineKeyboardButton(text=f"{ICON.get(e['_src'], '🎵')} {title}{dur}",
                                   callback_data=f"dl:{remember(e)}"))
    await message.answer("Не тот трек? Другие варианты (нажми, чтобы получить аудио):",
                         reply_markup=b.as_markup())


def links_block(artist: str, title: str) -> str:
    from urllib.parse import quote_plus
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
    """Показывает инфо о найденном треке и отправляет аудиофайл с обложкой."""
    info_text = format_track(track)
    title, artist = track.get("title", "Track"), track.get("subtitle", "")
    await status.edit_text(info_text + "\n\n⬇️ Скачиваю аудио...", disable_web_page_preview=True)
    images = track.get("images") or {}
    cover_url = images.get("coverarthq") or images.get("coverart")
    try:
        entries = await asyncio.to_thread(_search, f"{artist} - {title}")
        if not entries:
            raise RuntimeError("не нашёл аудио в интернете")
        with tempfile.TemporaryDirectory() as tmp:
            mp3, info, _ = await asyncio.to_thread(_download_best, entries, tmp)
            await finalize_and_send(message, tmp, mp3, title, artist,
                                    cover_url or info.get("thumbnail"))
    except Exception as e:
        logging.exception("deliver error")
        await message.answer("⚠️ Песню нашёл, но скачать аудио не получилось: "
                             f"{html.escape(explain_error(e))}")
    await status.edit_text(info_text, disable_web_page_preview=True)


# =====================  /start И МЕНЮ  =====================

def start_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔎 Найти песню", callback_data="menu:search"))
    b.row(InlineKeyboardButton(text="🎛 Эффекты", callback_data="menu:fx"))
    b.row(InlineKeyboardButton(text="🏷 Теги и обложка", callback_data="menu:tags"))
    b.row(InlineKeyboardButton(text="📢 Пост в канал", callback_data="menu:post"))
    return b.as_markup()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Выбери функцию кнопкой ниже или просто пришли мне:\n\n"
        "• <b>аудиофайл</b> — сделаю эффекты, поменяю теги и обложку\n"
        "• <b>голосовое, видео, кружок или ссылку</b> — найду песню и пришлю аудио\n"
        "• <b>название</b> — найду трек и пришлю аудио",
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
        await cb.message.answer("🏷 Пришли аудиофайл. В меню под ним будут кнопки «Теги» и «Обложка»: "
                                "их можно менять и без эффектов.")
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


# =====================  ВВОД ЗНАЧЕНИЙ (скорость / теги / обложка)  =====================

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


@dp.message(Fx.cover, F.photo | F.document.mime_type.startswith("image/"))
async def on_cover_input(message: Message, state: FSMContext):
    data = await state.get_data()
    s = SESSIONS.get(data.get("sid"))
    await state.clear()
    if not s:
        await message.reply("Сессия устарела, пришли файл заново.")
        return
    s.cover_file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    s.tags_changed = True
    await message.answer(menu_text(s), reply_markup=effects_kb(data["sid"], s))


@dp.message(Fx.cover, F.text)
async def on_cover_wrong(message: Message):
    await message.reply("Пришли картинку (как фото или файлом). Отмена: /cancel")


# =====================  АВТООПРЕДЕЛЕНИЕ ТИПА СООБЩЕНИЯ  =====================

# аудиофайл -> эффекты, теги, обложка
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


# текст: ссылка -> поиск песни, иначе поиск по названию (сразу присылает аудио)
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
    except Exception as e:
        logging.exception("url error")
        await status.edit_text(f"⚠️ Не получилось обработать ссылку: {html.escape(explain_error(e))}")
        return

    entries = await asyncio.to_thread(_search, text)
    if not entries:
        await status.edit_text("😕 Ничего не нашёл.")
        return
    await status.edit_text("⬇️ Нашёл, скачиваю аудио...")
    used = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp3, info, used = await asyncio.to_thread(_download_best, entries, tmp)
            title, artist = guess_tags(info)
            await finalize_and_send(message, tmp, mp3, title, artist, info.get("thumbnail"))
        await status.delete()
    except Exception as e:
        logging.exception("search download error")
        await status.edit_text(f"⚠️ Не получилось скачать аудио: {html.escape(explain_error(e))}")
    await send_alternatives(message, entries, used)


# =====================  КНОПКИ: скачать трек / эффекты  =====================

@dp.callback_query(F.data.startswith("dl:"))
async def on_download(cb: CallbackQuery):
    url = RESULTS.get(cb.data.split(":", 1)[1])
    if not url:
        await cb.answer("Результат устарел, повтори поиск", show_alert=True)
        return
    await cb.answer("Скачиваю...")
    status = await cb.message.answer("⬇️ Скачиваю аудио...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp3, info = await asyncio.to_thread(_download_mp3, url, tmp)
            title, artist = guess_tags(info)
            await finalize_and_send(cb.message, tmp, mp3, title, artist, info.get("thumbnail"))
        await status.delete()
    except Exception as e:
        logging.exception("download error")
        await status.edit_text(f"⚠️ Не получилось скачать: {html.escape(explain_error(e))}")


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
    elif action == "cover":
        await state.set_state(Fx.cover)
        await state.update_data(sid=sid)
        await cb.message.answer("🖼 Пришли картинку для обложки (фото или файл-изображение). "
                                "Я обрежу её до квадрата и вставлю в трек.")
    elif action == "go":
        if not s.has_effects and not s.tags_changed:
            await cb.answer("Выбери эффект или измени теги / обложку", show_alert=True)
            return
        await cb.answer()
        await render(cb, bot, sid, s)
        return
    await cb.answer()


async def render(cb: CallbackQuery, bot: Bot, sid: str, s: Session) -> None:
    await cb.message.edit_text("⏳ Обрабатываю, это может занять до минуты...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input"
            await bot.download(s.file_id, destination=src)
            cover = thumb = None
            if s.cover_file_id:
                raw = tmp_dir / "cover_raw"
                await bot.download(s.cover_file_id, destination=raw)
                cover, thumb = await prepare_cover(raw, tmp_dir)
            out = tmp_dir / "result.mp3"

            async def build(keep: bool) -> None:
                if s.has_effects:
                    await apply_effects(src, out, s, cover, keep)
                else:                                   # только теги и обложка
                    await retag(src, out, s.title, s.artist, copy=(s.ext == ".mp3"),
                                cover=cover, keep_cover=keep)

            try:
                await build(True)
            except RuntimeError:                        # старая обложка файла могла помешать
                await build(False)
            ok = await send_mp3(cb.message, out, s.title, s.artist, thumb)
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
