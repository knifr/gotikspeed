"""Telegram-бот.

Логика:
  • аудиофайл (mp3, m4a, wav...)   -> меню эффектов: speed up / slowed, reverb, 8D
  • голосовое, видео, кружок       -> поиск песни (Shazam)
  • ссылка на видео/трек           -> поиск песни (Shazam)
  • обычный текст                  -> поиск по названию
"""
import asyncio
import html
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from shazamio import Shazam
from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")

MAX_TG_DOWNLOAD = 20 * 1024 * 1024   # лимит Bot API на скачивание файлов
MAX_TG_UPLOAD = 50 * 1024 * 1024     # лимит Bot API на отправку файлов
SAMPLE_SECONDS = 25                  # фрагмент для распознавания
MIN_SPEED, MAX_SPEED = 0.25, 3.0
URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
shazam = Shazam()


# =====================  ЭФФЕКТЫ  =====================

@dataclass
class Session:
    user_id: int
    file_id: str
    title: str = "Track"
    speed: float = 1.0
    reverb: bool = False
    eightd: bool = False


SESSIONS: dict[str, Session] = {}
AWAITING_CUSTOM: dict[int, str] = {}   # user_id -> sid (ждём ручной ввод скорости)
PRESETS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]


def new_session(user_id: int, file_id: str, title: str) -> str:
    sid = uuid.uuid4().hex[:8]
    SESSIONS[sid] = Session(user_id, file_id, title)
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
        f"🎛 <b>{html.escape(s.title)}</b>\nВыбери эффекты:\n\n"
        f"⏱ Скорость: <b>{speed}</b>\n"
        f"🌊 Reverb: <b>{'вкл' if s.reverb else 'выкл'}</b>\n"
        f"🎧 8D: <b>{'вкл' if s.eightd else 'выкл'}</b> (слушай в наушниках)\n\n"
        "Скорость меняет и тон, как в обычных slowed / sped up ремиксах. "
        "Эффекты можно комбинировать. Потом жми «Создать»."
    )


def effects_label(s: Session) -> str:
    parts = []
    if s.speed < 1:
        parts.append(f"slowed {s.speed:g}x")
    elif s.speed > 1:
        parts.append(f"sped up {s.speed:g}x")
    if s.reverb:
        parts.append("reverb")
    if s.eightd:
        parts.append("8D")
    return " + ".join(parts)


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


async def run_ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg не смог обработать файл")


async def apply_effects(src: Path, dst: Path, s: Session) -> None:
    await run_ffmpeg("-i", str(src), "-vn", "-ac", "2", "-af", build_filter(s),
                     "-c:a", "libmp3lame", "-b:a", "192k", str(dst))


# =====================  ПОИСК ПЕСНИ  =====================

async def make_sample(src: Path, dst: Path) -> None:
    await run_ffmpeg("-i", str(src), "-t", str(SAMPLE_SECONDS),
                     "-vn", "-ac", "1", "-ar", "44100", str(dst))


def _download_from_url(url: str, out_dir: str) -> Path:
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "dl.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "download_ranges": download_range_func(None, [(0, 60)]),  # только первая минута
        "max_filesize": 100 * 1024 * 1024,
    }
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(out_dir).glob("dl.*") if p.suffix != ".part"]
    if not files:
        raise RuntimeError("Не удалось скачать по ссылке")
    return files[0]


def _search_youtube(query: str) -> list[dict]:
    with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
    return info.get("entries", []) if info else []


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


async def recognize_file(path: Path) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "sample.wav"
        await make_sample(path, sample)
        result = await shazam.recognize(str(sample))
    track = result.get("track")
    return format_track(track) if track else None


# =====================  ХЕНДЛЕРЫ  =====================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Я умею два трюка 🎧\n\n"
        "<b>🔎 Найти песню</b> — пришли голосовое, видео, кружок или ссылку "
        "(YouTube, TikTok, Instagram и др.). Можно просто написать название.\n\n"
        "<b>🎛 Сделать ремикс</b> — пришли аудиофайл, и я добавлю speed up / slowed, "
        "reverb или 8D."
    )


# ---- аудиофайл -> эффекты ----
@dp.message(F.audio | F.document.mime_type.startswith("audio/"))
async def on_audio_file(message: Message):
    media = message.audio or message.document
    if media.file_size and media.file_size > MAX_TG_DOWNLOAD:
        await message.reply("Файл больше 20 МБ, Telegram не даёт мне его скачать.")
        return
    name = getattr(media, "title", None) or Path(media.file_name or "Track").stem
    sid = new_session(message.from_user.id, media.file_id, name)
    await message.reply(menu_text(SESSIONS[sid]), reply_markup=effects_kb(sid, SESSIONS[sid]))


# ---- гс / видео / кружок -> поиск песни ----
@dp.message(F.voice | F.video | F.video_note)
async def on_media_recognize(message: Message, bot: Bot):
    media = message.voice or message.video or message.video_note
    if media.file_size and media.file_size > MAX_TG_DOWNLOAD:
        await message.reply("Файл больше 20 МБ, Telegram не даёт мне его скачать. Пришли покороче.")
        return
    status = await message.reply("🎧 Слушаю...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            await bot.download(media.file_id, destination=src)
            answer = await recognize_file(src)
    except Exception:
        logging.exception("media error")
        answer = "⚠️ Не получилось обработать файл."
    await status.edit_text(
        answer or "😕 Не смог узнать трек. Попробуй фрагмент, где музыка слышна чётче.",
        disable_web_page_preview=True,
    )


# ---- текст: ручная скорость / ссылка / поиск по названию ----
@dp.message(F.text)
async def on_text(message: Message):
    text = message.text.strip()
    uid = message.from_user.id

    if uid in AWAITING_CUSTOM:                       # пользователь вводит свою скорость
        sid = AWAITING_CUSTOM.pop(uid)
        s = SESSIONS.get(sid)
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            value = None
        if s and value is not None and MIN_SPEED <= value <= MAX_SPEED:
            s.speed = value
            await message.answer(menu_text(s), reply_markup=effects_kb(sid, s))
        else:
            await message.reply(f"Нужно число от {MIN_SPEED:g} до {MAX_SPEED:g}, например 0.85. "
                                "Нажми «✏️ своё» ещё раз.")
        return

    url_match = URL_RE.search(text)
    status = await message.reply("🔎 Ищу...")
    try:
        if url_match:                                # ссылка -> распознаём
            with tempfile.TemporaryDirectory() as tmp:
                path = await asyncio.to_thread(_download_from_url, url_match.group(0), tmp)
                answer = await recognize_file(path)
            answer = answer or "😕 Не смог узнать трек по этой ссылке."
        else:                                        # название -> поиск
            entries = await asyncio.to_thread(_search_youtube, text)
            if not entries:
                answer = "😕 Ничего не нашёл."
            else:
                lines = [f"🔎 Результаты по запросу <b>{html.escape(text)}</b>:\n"]
                for i, e in enumerate(entries, 1):
                    lines.append(f'{i}. <a href="{e["url"]}">{html.escape(e.get("title", "без названия"))}</a>')
                lines.append("\n" + links_block("", text))
                answer = "\n".join(lines)
    except Exception:
        logging.exception("text error")
        answer = "⚠️ Что-то пошло не так. Проверь ссылку или попробуй позже."
    await status.edit_text(answer, disable_web_page_preview=True)


# ---- кнопки эффектов ----
async def refresh_menu(cb: CallbackQuery, sid: str, s: Session) -> None:
    try:
        await cb.message.edit_text(menu_text(s), reply_markup=effects_kb(sid, s))
    except TelegramBadRequest:
        pass  # "message is not modified"


@dp.callback_query(F.data.startswith("fx:"))
async def on_fx(cb: CallbackQuery, bot: Bot):
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
        AWAITING_CUSTOM[cb.from_user.id] = sid
        await cb.message.answer(f"Напиши скорость числом от {MIN_SPEED:g} до {MAX_SPEED:g}, "
                                "например 0.85 или 1.3")
    elif action == "go":
        if abs(s.speed - 1) < 1e-9 and not s.reverb and not s.eightd:
            await cb.answer("Выбери хотя бы один эффект", show_alert=True)
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
            out = Path(tmp) / "remix.mp3"
            await apply_effects(src, out, s)
            if out.stat().st_size > MAX_TG_UPLOAD:
                await cb.message.edit_text("⚠️ Результат больше 50 МБ, Telegram не даёт его отправить. "
                                           "Возьми трек покороче.", reply_markup=effects_kb(sid, s))
                return
            label = effects_label(s)
            await cb.message.answer_audio(
                FSInputFile(out, filename=f"{s.title} ({label}).mp3"),
                title=f"{s.title} ({label})", performer="Music Finder Bot",
            )
        await cb.message.edit_text("✅ Готово! Можешь выбрать другие эффекты:\n\n" + menu_text(s),
                                   reply_markup=effects_kb(sid, s))
    except Exception:
        logging.exception("render error")
        await cb.message.edit_text("⚠️ Не получилось сделать ремикс. Попробуй другой файл.",
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
    await start_health_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
