# Music Finder Bot

Telegram-бот с двумя функциями.

| Что прислать | Что делает бот |
|---|---|
| Аудиофайл (mp3, m4a, wav...) | Меню эффектов: speed up / slowed, reverb, 8D |
| Голосовое, видео, кружок | Ищет песню (Shazam) |
| Ссылка (YouTube, TikTok, Instagram...) | Ищет песню (Shazam) |
| Текст | Поиск по названию |

Распознавание: [ShazamIO](https://github.com/shazamio/ShazamIO). Загрузка по ссылкам: [yt-dlp](https://github.com/yt-dlp/yt-dlp). Обработка звука: ffmpeg.

## Эффекты

- **Скорость**: пресеты 0.5x–0.9x (slowed), 1.1x–2x (speed up) или своё значение от 0.25 до 3. Тон меняется вместе со скоростью, как в обычных slowed / sped up ремиксах.
- **Reverb**: эхо и реверберация.
- **8D**: звук «кружится» вокруг головы (лучше слушать в наушниках).
- Эффекты можно комбинировать, например `0.8x + Reverb`. Результат приходит в mp3 (192 kbps).

## Локальный запуск

1. Python 3.10–3.12 и [ffmpeg](https://ffmpeg.org/download.html) в PATH.
2. Токен от [@BotFather](https://t.me/BotFather) (`/newbot`).

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # впиши BOT_TOKEN
python bot.py
```

## Деплой на Render

В проекте есть `Dockerfile` (с ffmpeg) и `render.yaml`.

1. Залей репозиторий на GitHub (`.env` в него не попадёт).
2. На [render.com](https://render.com): **New → Blueprint** (или **New → Background Worker**, runtime **Docker**) и выбери репозиторий.
3. В переменных окружения добавь `BOT_TOKEN`.
4. Нажми Deploy. Логи покажут, что бот запустился.

Background Worker на Render платный. Если нужен бесплатный вариант, создай **Web Service** (Docker, план Free). Бот сам поднимет мини-сервер на порту из `PORT`, чтобы Render видел, что сервис жив. Но бесплатный Web Service засыпает через 15 минут без запросов, поэтому понадобится внешний пингер (например, UptimeRobot), который будет раз в 10 минут открывать адрес сервиса.

## Ограничения

- Telegram не даёт боту скачивать файлы больше 20 МБ и отправлять больше 50 МБ.
- Для ссылок анализируется первая минута видео.
- Если ссылки перестали скачиваться, обнови yt-dlp: `pip install -U yt-dlp` (на Render достаточно пересобрать образ).
- Сессии эффектов хранятся в памяти: после перезапуска старые кнопки перестанут работать.
- Обработка чужих треков может нарушать авторские права, ответственность на том, кто их использует.
